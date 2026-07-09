"""NMC satellite image fetching.

The NMC satellite pages expose the newest frame and timeline frames in
the page HTML as ``data-img`` / ``data-time`` attributes.  This module
keeps the implementation page-driven so the command follows NMC's
current image URLs instead of constructing product names manually.
"""

from __future__ import annotations

import asyncio
import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import httpx

_LOG = logging.getLogger(__name__)

_BASE = "https://www.nmc.cn"

_DEFAULT_PAGE = "/publish/satellite/China_Northwest_Pacific_Ocean.html"
_DEFAULT_LABEL = "海区红外云图"

_ROUTES: dict[str, tuple[str, str]] = {
    "海区红外云图": (_DEFAULT_PAGE, _DEFAULT_LABEL),
    "海区红外": (_DEFAULT_PAGE, _DEFAULT_LABEL),
    "西北太平洋": (_DEFAULT_PAGE, "西北太平洋海区红外云图"),
    "西北太平洋海区": (_DEFAULT_PAGE, "西北太平洋海区红外云图"),
    "中国近海": (_DEFAULT_PAGE, "中国近海海区红外云图"),
    "近海": (_DEFAULT_PAGE, "中国近海海区红外云图"),
    "海区": (_DEFAULT_PAGE, _DEFAULT_LABEL),
}

_IMAGE_URL_RE = re.compile(
    r"image\.nmc\.cn/product/.*?WXSP.*?\.(?:png|jpg|jpeg)(?:\?.*)?$",
    re.IGNORECASE,
)


class _SatelliteFrameParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[dict[str, str]] = []
        self._frame_by_url: dict[str, dict[str, str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        image_url = attr_map.get("data-img") or attr_map.get("src")
        if not image_url:
            return

        image_url = _normalize_url(image_url)
        if not _IMAGE_URL_RE.search(image_url):
            return

        time = attr_map.get("data-time", "")
        existing = self._frame_by_url.get(image_url)
        if existing is not None:
            if time and not existing["time"]:
                existing["time"] = time
            return

        frame = {"url": image_url, "time": time}
        self._frame_by_url[image_url] = frame
        self.frames.append(frame)


async def _fetch_page(path: str) -> str:
    """Fetch an NMC satellite page and return the HTML body."""
    url = urljoin(_BASE, path)
    async with httpx.AsyncClient() as client:
        res = await client.get(url, timeout=15.0, follow_redirects=True)
        res.raise_for_status()
    return res.text


def _normalize_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return urljoin(_BASE, url)


def _parse_latest(html: str) -> tuple[str, str]:
    """Extract the newest ``(image_url, obs_time)`` from a satellite page."""
    frames = _parse_frames(html)
    if not frames:
        raise ValueError("页面中未找到海区云图")
    latest = frames[0]
    return latest["url"], latest["time"]


def _parse_frames(html: str) -> list[dict[str, str]]:
    """Extract all satellite frame URLs and times, ordered newest-first."""
    parser = _SatelliteFrameParser()
    parser.feed(html)
    return parser.frames


def resolve_satellite_page(query: str) -> tuple[str, str]:
    """Map a user query to an NMC satellite page path and label."""
    q = query.strip()
    if not q:
        return _DEFAULT_PAGE, _DEFAULT_LABEL

    if q in _ROUTES:
        return _ROUTES[q]

    for key in sorted(_ROUTES, key=len, reverse=True):
        if key in q or q in key:
            return _ROUTES[key]

    return _DEFAULT_PAGE, q


async def fetch_satellite(query: str = "") -> tuple[str, str, str]:
    """Fetch the latest satellite image for *query*.

    Returns ``(image_url, obs_time, label)``.
    """
    page_path, label = resolve_satellite_page(query)
    html = await _fetch_page(page_path)
    url, obs_time = _parse_latest(html)
    return url, obs_time, label


async def download_satellite_image(url: str, dest: Path) -> None:
    """Download a satellite image from *url* to *dest*."""
    async with httpx.AsyncClient() as client:
        res = await client.get(url, timeout=30.0, follow_redirects=True)
        res.raise_for_status()
    dest.write_bytes(res.content)


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

    return list(await asyncio.gather(*(_one(url) for url in subset)))


async def fetch_satellite_gif(
    query: str = "", *, max_frames: int = 20
) -> tuple[bytes, str, str, str]:
    """Fetch satellite frames and return an animated GIF.

    Returns ``(gif_bytes, newest_time, oldest_time, label)``.
    """
    from shinbot_plugin_renderkit import GifRenderOptions, render_frames_to_gif

    page_path, label = resolve_satellite_page(query)
    html = await _fetch_page(page_path)
    frames = _parse_frames(html)
    if not frames:
        raise ValueError("页面中未找到海区云图")

    frames = list(reversed(frames))
    urls = [frame["url"] for frame in frames]
    oldest_time = frames[0].get("time", "")
    newest_time = frames[-1].get("time", "")

    raw_frames = await _download_all_frames(urls, max_frames=max_frames)
    gif_bytes = await render_frames_to_gif(raw_frames, options=GifRenderOptions(fps=5))
    return gif_bytes, newest_time, oldest_time, label
