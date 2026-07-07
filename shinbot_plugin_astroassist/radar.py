"""NMC (中央气象台) radar echo image fetching.

Supports three radar types — the implementation is **page-driven**:
we fetch the appropriate NMC HTML page and extract ``data-img`` URLs
directly, rather than trying to construct image URLs from scratch.
This handles variable time intervals and differing URL patterns
automatically.

Provides both static PNG (latest frame) and animated GIF (all frames
from the page, ~2 h of history).
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import httpx

_LOG = logging.getLogger(__name__)

_BASE = "http://www.nmc.cn"
_GIF_FPS = 2  # frames per second (matches 6-min observation interval well)

# ------------------------------------------------------------------
# Route table: keyword → NMC radar page path
#
# Order matters — longer/more-specific keys first so that a substring
# match on "北京" doesn't swallow "北京天文馆".
# ------------------------------------------------------------------
_ROUTES: dict[str, str] = {
    # --- Regional mosaics (区域拼图) ---
    "全国": "/publish/radar/chinaall.html",
    "华北": "/publish/radar/huabei.html",
    "东北": "/publish/radar/dongbei.html",
    "华东": "/publish/radar/huadong.html",
    "华中": "/publish/radar/huazhong.html",
    "华南": "/publish/radar/huanan.html",
    "西南": "/publish/radar/xinan.html",
    "西北": "/publish/radar/xibei.html",
    # --- Province / city single-station pages ---
    "北京": "/publish/radar/bei-jing/da-xing.htm",
    "天津": "/publish/radar/tian-jin/tian-jin.htm",
    "河北": "/publish/radar/he-bei/shi-jia-zhuang.htm",
    "石家庄": "/publish/radar/he-bei/shi-jia-zhuang.htm",
    "山西": "/publish/radar/shan-xi/tai-yuan.htm",
    "太原": "/publish/radar/shan-xi/tai-yuan.htm",
    "内蒙古": "/publish/radar/nei-meng/e-er-duo-si.htm",
    "鄂尔多斯": "/publish/radar/nei-meng/e-er-duo-si.htm",
    "辽宁": "/publish/radar/liao-ning/shen-yang.htm",
    "沈阳": "/publish/radar/liao-ning/shen-yang.htm",
    "吉林": "/publish/radar/ji-lin/chang-chun.htm",
    "长春": "/publish/radar/ji-lin/chang-chun.htm",
    "黑龙江": "/publish/radar/hei-long-jiang/ha-er-bin.htm",
    "哈尔滨": "/publish/radar/hei-long-jiang/ha-er-bin.htm",
    "上海": "/publish/radar/shang-hai/qing-pu.htm",
    "青浦": "/publish/radar/shang-hai/qing-pu.htm",
    "江苏": "/publish/radar/jiang-su/nan-jing.htm",
    "南京": "/publish/radar/jiang-su/nan-jing.htm",
    "浙江": "/publish/radar/zhe-jiang/hang-zhou.htm",
    "杭州": "/publish/radar/zhe-jiang/hang-zhou.htm",
    "安徽": "/publish/radar/an-hui/he-fei.htm",
    "合肥": "/publish/radar/an-hui/he-fei.htm",
    "福建": "/publish/radar/fu-jian/fu-zhou.htm",
    "福州": "/publish/radar/fu-jian/fu-zhou.htm",
    "江西": "/publish/radar/jiang-xi/nan-chang.htm",
    "南昌": "/publish/radar/jiang-xi/nan-chang.htm",
    "山东": "/publish/radar/shan-dong/ji-nan.htm",
    "济南": "/publish/radar/shan-dong/ji-nan.htm",
    "河南": "/publish/radar/he-nan/shang-qiu.htm",
    "商丘": "/publish/radar/he-nan/shang-qiu.htm",
    "湖北": "/publish/radar/hu-bei/wu-han.htm",
    "武汉": "/publish/radar/hu-bei/wu-han.htm",
    "湖南": "/publish/radar/hu-nan/chang-sha.htm",
    "长沙": "/publish/radar/hu-nan/chang-sha.htm",
    "广西": "/publish/radar/guang-xi/gui-lin.htm",
    "桂林": "/publish/radar/guang-xi/gui-lin.htm",
    "海南": "/publish/radar/hai-nan/hai-kou.htm",
    "海口": "/publish/radar/hai-nan/hai-kou.htm",
    "重庆": "/publish/radar/chong-qing/chong-qing.htm",
    "四川": "/publish/radar/si-chuan/cheng-du.htm",
    "成都": "/publish/radar/si-chuan/cheng-du.htm",
    "贵州": "/publish/radar/gui-zhou/gui-yang.htm",
    "贵阳": "/publish/radar/gui-zhou/gui-yang.htm",
    "西藏": "/publish/radar/xi-cang/la-sa.htm",
    "拉萨": "/publish/radar/xi-cang/la-sa.htm",
    "陕西": "/publish/radar/shan-xi/xi-an.htm",
    "西安": "/publish/radar/shan-xi/xi-an.htm",
    "甘肃": "/publish/radar/gan-su/lan-zhou.htm",
    "兰州": "/publish/radar/gan-su/lan-zhou.htm",
    "青海": "/publish/radar/qing-hai/xi-ning.htm",
    "西宁": "/publish/radar/qing-hai/xi-ning.htm",
    "宁夏": "/publish/radar/ning-xia/yin-chuan.htm",
    "银川": "/publish/radar/ning-xia/yin-chuan.htm",
}

# Aliases / short names that don't collide with province names
_EXTRA_ALIASES: dict[str, str] = {
    "广东上川岛": "/publish/tianqishikuang/leidatu/danzhanleida/guangdong/shangchuandao/index.html",
    "上川岛": "/publish/tianqishikuang/leidatu/danzhanleida/guangdong/shangchuandao/index.html",
    "云南曲靖": "/publish/tianqishikuang/leidatu/danzhanleida/yunnan/qujing/index.html",
    "曲靖": "/publish/tianqishikuang/leidatu/danzhanleida/yunnan/qujing/index.html",
    "新疆塔城": "/publish/tianqishikuang/leidatu/danzhanleida/xinjiang/tacheng/index.html",
    "塔城": "/publish/tianqishikuang/leidatu/danzhanleida/xinjiang/tacheng/index.html",
    "海坨山": "/publish/tianqishikuang/leidatu/danzhanleida/beijing/haituoshan/index.html",
    "大兴": "/publish/radar/bei-jing/da-xing.htm",
}

# ------------------------------------------------------------------
# HTML parsing
# ------------------------------------------------------------------

# data-img="https://image.nmc.cn/product/...RDCP/....PNG?v=..."
_IMG_RE = re.compile(
    r'data-img="(https://image\.nmc\.cn/product/[^"]+?RDCP[^"]*?\.PNG\?v=\d+)"'
)
_TIME_RE = re.compile(r'data-time="([^"]+)"')


# ------------------------------------------------------------------
# Page-level fetch helpers
# ------------------------------------------------------------------


async def _fetch_page(path: str) -> str:
    """Fetch an NMC page and return its HTML body."""
    url = _BASE + path
    async with httpx.AsyncClient() as client:
        res = await client.get(url, timeout=15.0, follow_redirects=True)
        res.raise_for_status()
    return res.text


def _parse_latest(html: str) -> tuple[str, str]:
    """Extract ``(image_url, obs_time)`` from a radar page's HTML.

    Picks the **first** ``data-img`` match (newest frame).
    Raises ``ValueError`` if the page contains no radar images.
    """
    urls = _IMG_RE.findall(html)
    if not urls:
        raise ValueError("页面中未找到雷达回波图")

    time_m = _TIME_RE.search(html)
    obs_time = time_m.group(1) if time_m else ""
    return urls[0], obs_time


def _parse_frames(html: str) -> list[dict[str, str]]:
    """Extract all ``(image_url, time)`` pairs for animation / history.

    Returns a list ordered newest-first, each dict containing
    ``{"url": str, "time": str}``.
    """
    urls = _IMG_RE.findall(html)
    times = _TIME_RE.findall(html)
    frames: list[dict[str, str]] = []
    for i, url in enumerate(urls):
        t = times[i] if i < len(times) else ""
        frames.append({"url": url, "time": t})
    return frames


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def resolve_radar_page(query: str) -> str:
    """Map a user query (e.g. "华北", "北京", "武汉") to an NMC page path.

    Search order:
    1. Exact match in ``_EXTRA_ALIASES`` (long-tail station names).
    2. Exact match in ``_ROUTES`` (provinces + regions).
    3. Substring match in ``_ROUTES`` (first hit wins).

    Returns ``"/publish/radar/chinaall.html"`` (national mosaic) as
    fallback when nothing matches.
    """
    q = query.strip()

    # 1. Extra aliases (exact)
    if q in _EXTRA_ALIASES:
        return _EXTRA_ALIASES[q]

    # 2. Exact match in main routes
    if q in _ROUTES:
        return _ROUTES[q]

    # 3. Substring match — prefer longer keys first
    for key in sorted(_ROUTES, key=len, reverse=True):
        if key in q or q in key:
            return _ROUTES[key]

    # Fallback: national mosaic
    return "/publish/radar/chinaall.html"


async def fetch_radar(query: str = "") -> tuple[str, str, str]:
    """High-level: fetch the latest radar image for *query*.

    Returns ``(image_url, obs_time, location_label)``.
    """
    page_path = resolve_radar_page(query) if query else "/publish/radar/chinaall.html"

    # Derive a human-readable label from the matched key
    label = "全国" if page_path == "/publish/radar/chinaall.html" else (query or "全国")

    html = await _fetch_page(page_path)
    url, obs_time = _parse_latest(html)
    return url, obs_time, label


async def download_radar_image(url: str, dest: Path) -> None:
    """Download a radar image from *url* and write it to *dest*."""
    async with httpx.AsyncClient() as client:
        res = await client.get(url, timeout=30.0, follow_redirects=True)
        res.raise_for_status()
    dest.write_bytes(res.content)


# ------------------------------------------------------------------
# Animated GIF
# ------------------------------------------------------------------


async def _download_all_frames(
    urls: list[str], *, max_frames: int = 20
) -> list[bytes]:
    """Download up to *max_frames* images concurrently, returning raw bytes."""
    subset = urls[:max_frames]
    sem = asyncio.Semaphore(6)

    async def _one(url: str) -> bytes:
        async with sem:
            async with httpx.AsyncClient() as client:
                res = await client.get(url, timeout=30.0, follow_redirects=True)
                res.raise_for_status()
                return res.content

    return list(await asyncio.gather(*(_one(u) for u in subset)))




async def fetch_radar_gif(
    query: str = "", *, max_frames: int = 20
) -> tuple[bytes, str, str, str]:
    """Fetch radar data and return an animated GIF.

    Returns ``(gif_bytes, newest_time, oldest_time, label)``.
    """
    from shinbot_plugin_renderkit import GifRenderOptions, render_frames_to_gif

    page_path = resolve_radar_page(query) if query else "/publish/radar/chinaall.html"
    label = "全国" if page_path == "/publish/radar/chinaall.html" else (query or "全国")

    html = await _fetch_page(page_path)
    frames = _parse_frames(html)
    if not frames:
        raise ValueError("页面中未找到雷达回波图")

    # Reverse to chronological order (oldest → newest) for animation
    frames = list(reversed(frames))

    urls = [f["url"] for f in frames]
    oldest_time = frames[0].get("time", "")
    newest_time = frames[-1].get("time", "")

    raw_frames = await _download_all_frames(urls, max_frames=max_frames)
    gif_bytes = await render_frames_to_gif(
        raw_frames,
        options=GifRenderOptions(fps=5),
    )

    return gif_bytes, newest_time, oldest_time, label
