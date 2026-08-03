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
from dataclasses import dataclass
import difflib
import logging
import math
import re
from pathlib import Path

import httpx

from .geo import GeocodeResult, amap_geocode, amap_geocode_detail

_LOG = logging.getLogger(__name__)

_BASE = "https://www.nmc.cn"
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


@dataclass(frozen=True)
class RadarStation:
    """A single NMC radar page discovered from its province navigation."""

    label: str
    path: str
    province: str


@dataclass(frozen=True)
class NearbyRadarStation:
    """A station candidate and its geocoded distance from the request."""

    station: RadarStation
    distance_km: float


# These are the province seed pages used by NMC's own "城市/地区" navigation.
# The station table itself is parsed from those pages, so newly added NMC
# stations do not require a code release.
_PROVINCE_RADAR_PAGES: dict[str, str] = {
    "北京": "/publish/radar/bei-jing/da-xing.htm",
    "天津": "/publish/radar/tian-jin/tian-jin.htm",
    "河北": "/publish/radar/he-bei/shi-jia-zhuang.htm",
    "山西": "/publish/radar/shan-xi/tai-yuan.htm",
    "内蒙古": "/publish/radar/nei-meng/e-er-duo-si.htm",
    "辽宁": "/publish/radar/liao-ning/shen-yang.htm",
    "吉林": "/publish/radar/ji-lin/chang-chun.htm",
    "黑龙江": "/publish/radar/hei-long-jiang/ha-er-bin.htm",
    "上海": "/publish/radar/shang-hai/qing-pu.htm",
    "江苏": "/publish/radar/jiang-su/nan-jing.htm",
    "浙江": "/publish/radar/zhe-jiang/hang-zhou.htm",
    "安徽": "/publish/radar/an-hui/he-fei.htm",
    "福建": "/publish/radar/fu-jian/fu-zhou.htm",
    "江西": "/publish/radar/jiang-xi/nan-chang.htm",
    "山东": "/publish/radar/shan-dong/ji-nan.htm",
    "河南": "/publish/radar/he-nan/shang-qiu.htm",
    "湖北": "/publish/radar/hu-bei/wu-han.htm",
    "湖南": "/publish/radar/hu-nan/chang-sha.htm",
    "广东": "/publish/radar/guang-dong/guang-zhou.htm",
    "广西": "/publish/radar/guang-xi/gui-lin.htm",
    "海南": "/publish/radar/hai-nan/hai-kou.htm",
    "重庆": "/publish/radar/chong-qing/chong-qing.htm",
    "四川": "/publish/radar/si-chuan/cheng-du.htm",
    "贵州": "/publish/radar/gui-zhou/gui-yang.htm",
    "云南": "/publish/radar/yun-nan/kun-ming.htm",
    "西藏": "/publish/radar/xi-cang/la-sa.htm",
    "陕西": "/publish/radar/shan-xi/xi-an.htm",
    "甘肃": "/publish/radar/gan-su/lan-zhou.htm",
    "青海": "/publish/radar/qing-hai/xi-ning.htm",
    "宁夏": "/publish/radar/ning-xia/yin-chuan.htm",
    "新疆": "/publish/radar/xin-jiang/wu-lu-mu-qi.htm",
}

_STATION_NAV_RE = re.compile(
    r'<div\b[^>]*class=["\'][^"\']*\bp-nav\b[^"\']*\bnav3\b[^"\']*["\']'
    r'[^>]*>(.*?)'
    r'</div>\s*</div>\s*<div\b[^>]*class=["\'][^"\']*\brow\b',
    re.DOTALL,
)
_STATION_LINK_RE = re.compile(
    r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>([^<]+)</a>'
)

_STATION_TABLE_CACHE: dict[str, tuple[RadarStation, ...]] = {}
_STATION_COORD_CACHE: dict[tuple[str, str], tuple[float, float]] = {}
_TARGET_COORD_CACHE: dict[str, GeocodeResult] = {}
_AUTO_NEARBY_RADIUS_KM = 180.0

# ------------------------------------------------------------------
# Pinyin / English aliases
#
# Keys are lowercase ASCII.  Where a romanisation is ambiguous (e.g.
# "shanxi" covers both 山西 and 陕西) it maps to the *more common*
# query; the other province should be written unambiguously.
# ------------------------------------------------------------------
_PINYIN_ALIASES: dict[str, str] = {
    # Regions
    "quanguo": "全国",
    "china": "全国",
    "national": "全国",
    "huabei": "华北",
    "north": "华北",
    "dongbei": "东北",
    "northeast": "东北",
    "huadong": "华东",
    "east": "华东",
    "huazhong": "华中",
    "central": "华中",
    "huanan": "华南",
    "south": "华南",
    "xinan": "西南",
    "southwest": "西南",
    "xibei": "西北",
    "northwest": "西北",
    # Provinces / cities
    "beijing": "北京",
    "bj": "北京",
    "tianjin": "天津",
    "tj": "天津",
    "hebei": "河北",
    "shijiazhuang": "石家庄",
    "shanxi": "山西",   # 山西 (Shanxi) — use "shaanxi" for 陕西
    "taiyuan": "太原",
    "neimenggu": "内蒙古",
    "innermongolia": "内蒙古",
    "erdos": "鄂尔多斯",
    "liaoning": "辽宁",
    "shenyang": "沈阳",
    "jilin": "吉林",
    "changchun": "长春",
    "heilongjiang": "黑龙江",
    "harbin": "哈尔滨",
    "haerbin": "哈尔滨",
    "shanghai": "上海",
    "sh": "上海",
    "qingpu": "青浦",
    "jiangsu": "江苏",
    "nanjing": "南京",
    "zhejiang": "浙江",
    "hangzhou": "杭州",
    "anhui": "安徽",
    "hefei": "合肥",
    "fujian": "福建",
    "fuzhou": "福州",
    "jiangxi": "江西",
    "nanchang": "南昌",
    "shandong": "山东",
    "jinan": "济南",
    "henan": "河南",
    "shangqiu": "商丘",
    "hubei": "湖北",
    "wuhan": "武汉",
    "hunan": "湖南",
    "changsha": "长沙",
    "guangdong": "广东",  # maps via _EXTRA_ALIASES fallback
    "guangxi": "广西",
    "guilin": "桂林",
    "hainan": "海南",
    "haikou": "海口",
    "chongqing": "重庆",
    "cq": "重庆",
    "sichuan": "四川",
    "chengdu": "成都",
    "guizhou": "贵州",
    "guiyang": "贵阳",
    "xizang": "西藏",
    "tibet": "西藏",
    "lasa": "拉萨",
    "lhasa": "拉萨",
    "shaanxi": "陕西",  # 陕西 (Shaanxi)
    "xian": "西安",
    "xi'an": "西安",
    "gansu": "甘肃",
    "lanzhou": "兰州",
    "qinghai": "青海",
    "xining": "西宁",
    "ningxia": "宁夏",
    "yinchuan": "银川",
    "xinjiang": "新疆",
    # Extra stations
    "qujing": "曲靖",
    "tacheng": "塔城",
    "haituoshan": "海坨山",
    "daxing": "大兴",
    "shangchuandao": "上川岛",
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


def resolve_radar_page(query: str) -> tuple[str, str] | None:
    """Map a user query to ``(page_path, canonical_label)``.

    Search order:
    1. Exact match in ``_EXTRA_ALIASES`` (long-tail station names).
    2. Exact match in ``_ROUTES`` (provinces + regions).
    3. Pinyin / English alias lookup (case-insensitive).
    4. Substring match in ``_ROUTES`` then ``_EXTRA_ALIASES``.

    Returns ``None`` when nothing matches.
    """
    q = query.strip()
    ql = q.lower()

    # 1. Extra aliases (exact Chinese)
    if q in _EXTRA_ALIASES:
        return _EXTRA_ALIASES[q], q

    # 2. Exact match in main routes (exact Chinese)
    if q in _ROUTES:
        return _ROUTES[q], q

    # 3. Pinyin / English alias (case-insensitive)
    if ql in _PINYIN_ALIASES:
        canonical = _PINYIN_ALIASES[ql]
        if canonical in _ROUTES:
            return _ROUTES[canonical], canonical
        if canonical in _EXTRA_ALIASES:
            return _EXTRA_ALIASES[canonical], canonical

    # 4. Substring match — prefer longer keys first
    for key in sorted(_ROUTES, key=len, reverse=True):
        if key in q or q in key:
            return _ROUTES[key], key
    for key in sorted(_EXTRA_ALIASES, key=len, reverse=True):
        if key in q or q in key:
            return _EXTRA_ALIASES[key], key

    return None


def suggest_radar_location(query: str) -> list[str]:
    """Return up to 3 fuzzy-matched location names for an unrecognised query.

    Tries Chinese name matching first; falls back to pinyin key matching.
    Only used to build a helpful error message.
    """
    q = query.strip()
    ql = q.lower()

    # Try against all Chinese keys
    chinese_keys = list(_ROUTES) + list(_EXTRA_ALIASES)
    hits = difflib.get_close_matches(q, chinese_keys, n=3, cutoff=0.4)
    if hits:
        return hits

    # Try against pinyin keys, map back to canonical Chinese
    pinyin_hits = difflib.get_close_matches(ql, list(_PINYIN_ALIASES), n=3, cutoff=0.6)
    seen: dict[str, None] = {}
    for h in pinyin_hits:
        seen[_PINYIN_ALIASES[h]] = None
    return list(seen)


def _normalize_province(value: str) -> str | None:
    """Map AMap's province/自治區 names to NMC's navigation labels."""
    province = value.strip()
    for canonical in _PROVINCE_RADAR_PAGES:
        if province == canonical or province.startswith(canonical):
            return canonical
    return None


def _parse_station_links(html: str, province: str) -> tuple[RadarStation, ...]:
    """Parse NMC's real station links from the province page navigation."""
    match = _STATION_NAV_RE.search(html)
    if match is None:
        return ()

    stations: list[RadarStation] = []
    seen_paths: set[str] = set()
    for path, label in _STATION_LINK_RE.findall(match.group(1)):
        path = path.strip()
        label = re.sub(r"\s+", " ", label).strip()
        if not label or path in seen_paths:
            continue
        if not (
            path.startswith("/publish/radar/")
            or path.startswith("/publish/tianqishikuang/leidatu/danzhanleida/")
        ):
            continue
        if path == "/publish/radar/chinaall.html":
            continue
        seen_paths.add(path)
        stations.append(RadarStation(label, path, province))
    return tuple(stations)


def _fallback_station_links(province: str) -> tuple[RadarStation, ...]:
    """Use the checked-in route aliases if NMC navigation is unavailable."""
    seed = _PROVINCE_RADAR_PAGES[province]
    stations: list[RadarStation] = []
    seen_paths: set[str] = set()
    for label, path in (*_ROUTES.items(), *_EXTRA_ALIASES.items()):
        # The checked-in table only guarantees each province's seed page.
        # Do not infer a station from a shared URL directory: 山西 and 陕西
        # both use ``shan-xi`` in NMC paths but are different provinces.
        if path != seed or path in seen_paths:
            continue
        # Province and seed-city aliases can point at the same page.  A city
        # label is more useful for distance/recommendation output.
        if label == province:
            continue
        seen_paths.add(path)
        stations.append(RadarStation(label, path, province))
    return tuple(stations)


async def _station_links_for_province(province: str) -> tuple[RadarStation, ...]:
    """Fetch and cache one province's current NMC station table."""
    cached = _STATION_TABLE_CACHE.get(province)
    if cached is not None:
        return cached

    try:
        html = await _fetch_page(_PROVINCE_RADAR_PAGES[province])
        stations = _parse_station_links(html, province)
    except Exception as exc:
        _LOG.debug("Radar station navigation unavailable for %s: %s", province, exc)
        stations = ()
    if not stations:
        stations = _fallback_station_links(province)
    _STATION_TABLE_CACHE[province] = stations
    return stations


def _haversine_km(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    """Return the great-circle distance between two WGS-84 coordinates."""
    earth_radius_km = 6371.0088
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = math.radians(latitude_b - latitude_a)
    delta_lon = math.radians(longitude_b - longitude_a)
    hav = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return earth_radius_km * 2 * math.asin(math.sqrt(hav))


def _rank_nearby_radar_stations(
    latitude: float,
    longitude: float,
    stations: list[RadarStation],
    station_coords: dict[tuple[str, str], tuple[float, float]],
    *,
    limit: int = 3,
) -> list[NearbyRadarStation]:
    """Rank geocoded NMC single-station pages by great-circle distance."""
    if limit <= 0:
        return []
    candidates = [
        NearbyRadarStation(
            station=station,
            distance_km=_haversine_km(
                latitude,
                longitude,
                station_coords[(station.province, station.label)][0],
                station_coords[(station.province, station.label)][1],
            ),
        )
        for station in stations
        if (station.province, station.label) in station_coords
    ]
    candidates.sort(key=lambda item: item.distance_km)
    return candidates[:limit]


async def _geocode_station_table(
    stations: tuple[RadarStation, ...],
    amap_key: str,
) -> dict[tuple[str, str], tuple[float, float]]:
    """Geocode station names concurrently, caching successful results."""
    semaphore = asyncio.Semaphore(5)

    async def geocode_station(station: RadarStation) -> None:
        cache_key = (station.province, station.label)
        if cache_key in _STATION_COORD_CACHE:
            return
        async with semaphore:
            try:
                latitude, longitude = await amap_geocode(
                    f"{station.province}{station.label}",
                    amap_key,
                )
            except Exception as exc:
                _LOG.debug("Radar station geocoding failed for %s: %s", cache_key, exc)
                return
            _STATION_COORD_CACHE[cache_key] = (latitude, longitude)

    await asyncio.gather(*(geocode_station(station) for station in stations))
    return {
        (station.province, station.label): _STATION_COORD_CACHE[(station.province, station.label)]
        for station in stations
        if (station.province, station.label) in _STATION_COORD_CACHE
    }


async def find_nearby_radar_stations(
    query: str,
    amap_key: str | None = None,
    *,
    limit: int = 3,
) -> list[NearbyRadarStation]:
    """Find current NMC stations near an unsupported place name.

    Both the requested place and candidate stations are geocoded through the
    existing AMap integration.  No station coordinate is handwritten in the
    plugin.  NMC's province navigation supplies the station names and URLs.
    """
    if not amap_key:
        return []

    cache_key = query.strip()
    try:
        target = _TARGET_COORD_CACHE.get(cache_key)
        if target is None:
            target = await amap_geocode_detail(cache_key, amap_key)
            _TARGET_COORD_CACHE[cache_key] = target
        province = _normalize_province(target.province)
        if province is None:
            return []
        stations = await _station_links_for_province(province)
        station_coords = await _geocode_station_table(stations, amap_key)
    except Exception as exc:
        _LOG.debug("Radar nearby lookup failed for %r: %s", query, exc)
        return []

    return _rank_nearby_radar_stations(
        target.latitude,
        target.longitude,
        list(stations),
        station_coords,
        limit=limit,
    )


def _format_nearby_stations(candidates: list[NearbyRadarStation]) -> str:
    """Format nearby station candidates for a concise user-facing hint."""
    return "、".join(
        f"{item.station.label}（约{item.distance_km:.0f} km）" for item in candidates
    )


async def _resolve_radar_request(
    query: str,
    *,
    amap_key: str | None = None,
) -> tuple[str, str]:
    """Resolve an exact, fuzzy, or geographic radar request."""
    if not query.strip():
        return "/publish/radar/chinaall.html", "全国"

    match = resolve_radar_page(query)
    # A province substring must not mask a more specific place, e.g.
    # "江苏无锡".  Try the geocoded nearby-station path first in that case;
    # if geocoding is unavailable, retaining the province route is a useful
    # compatibility fallback.
    try_nearby_first = bool(
        match
        and match[1] in _PROVINCE_RADAR_PAGES
        and query.strip() not in {
            match[1],
            f"{match[1]}省",
            f"{match[1]}市",
            f"{match[1]}自治区",
        }
    )
    if match is not None and not try_nearby_first:
        return match

    nearby = await find_nearby_radar_stations(query, amap_key)
    if nearby and nearby[0].distance_km <= _AUTO_NEARBY_RADIUS_KM:
        nearest = nearby[0]
        label = (
            f"{nearest.station.label}（{query.strip()}附近，"
            f"约{nearest.distance_km:.0f} km）"
        )
        return nearest.station.path, label

    if nearby:
        raise ValueError(
            f"未找到地名「{query}」对应的雷达站点。"
            f"可尝试临近站点：{_format_nearby_stations(nearby)}。"
        )

    if match is not None:
        return match

    suggestions = suggest_radar_location(query)
    hint = f"您是否想查询：{'、'.join(suggestions)}？" if suggestions else _known_locations()
    if not amap_key:
        hint += "（配置 amap_key 后可按地理位置推荐临近站点）"
    raise ValueError(f"未识别的地名「{query}」。{hint}")


def _known_locations() -> str:
    """Return a compact sample of known query terms for error messages."""
    regions = [k for k in _ROUTES if len(k) == 2 and "全" not in k][:8]
    return "、".join(regions) + ' 等省市/区域，或直接输入"全国"'


async def fetch_radar(
    query: str = "", *, amap_key: str | None = None
) -> tuple[str, str, str]:
    """High-level: fetch the latest radar image for *query*.

    Returns ``(image_url, obs_time, location_label)``.
    Raises ``ValueError`` when *query* does not match any known location.
    """
    page_path, label = await _resolve_radar_request(query, amap_key=amap_key)

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
    query: str = "", *, max_frames: int = 20, amap_key: str | None = None
) -> tuple[bytes, str, str, str]:
    """Fetch radar data and return an animated GIF.

    Returns ``(gif_bytes, newest_time, oldest_time, label)``.
    """
    from shinbot_plugin_renderkit import GifRenderOptions, render_frames_to_gif

    page_path, label = await _resolve_radar_request(query, amap_key=amap_key)

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
