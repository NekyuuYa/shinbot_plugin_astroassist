"""Dapiya tropical cyclone floater imagery fetching."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx

_API_BASE = "https://api.dapiya.cn"
_DATA_BASE = "https://data.dapiya.cn"
_DEFAULT_PRODUCT = "VIS"
_ALLOWED_PRODUCTS = {"VIS", "RGB", "TRUECOLOR"}


@dataclass(slots=True, frozen=True)
class DapiyaFloaterStorm:
    """One active tropical cyclone entry from Dapiya."""

    storm_id: str
    name: str = ""
    raw: str = ""
    group: str = ""


@dataclass(slots=True, frozen=True)
class DapiyaFloaterFrame:
    """One Dapiya floater image frame."""

    storm_id: str
    name: str
    product: str
    url: str
    time: str = ""


class DapiyaFloaterError(ValueError):
    """Raised when Dapiya floater data cannot satisfy a request."""


def normalize_dapiya_product(value: str = "") -> str:
    """Normalize and validate a supported Dapiya image product."""
    product = re.sub(r"[\s_-]+", "", (value or _DEFAULT_PRODUCT).strip().upper())
    if product == "TRUE":
        product = "TRUECOLOR"
    if product not in _ALLOWED_PRODUCTS:
        raise DapiyaFloaterError(
            f"不支持的台风云图类型：{value or product}，可选 VIS/RGB/TRUECOLOR"
        )
    return product


def parse_dapiya_active_storms(source: str) -> list[DapiyaFloaterStorm]:
    """Parse ``/typhoon/meso/all`` response text."""
    text = source.strip()
    if not text or text == "NO ATCF DATA":
        return []

    storms: list[DapiyaFloaterStorm] = []
    for line_index, line in enumerate(text.splitlines()):
        group = "meso" if line_index == 0 else "floater"
        for item in line.split("|"):
            raw = item.strip()
            if not raw:
                continue
            storm_id = raw.split(".", 1)[0][:3].upper()
            name = raw.split(".", 1)[1].strip() if "." in raw else ""
            storms.append(
                DapiyaFloaterStorm(storm_id=storm_id, name=name, raw=raw, group=group)
            )
    return storms


def resolve_dapiya_storm(
    storms: list[DapiyaFloaterStorm], query: str = ""
) -> DapiyaFloaterStorm:
    """Resolve a user query to an active Dapiya storm."""
    if not storms:
        raise DapiyaFloaterError("Dapiya 当前没有活跃热带气旋云图")

    text = query.strip()
    if not text:
        return storms[0]

    normalized_query = _normalize_query(text)
    fallback_ids = _storm_id_candidates(text)
    for storm in storms:
        candidates = {
            _normalize_query(storm.storm_id),
            _normalize_query(storm.name),
            _normalize_query(storm.raw),
        }
        if normalized_query in candidates:
            return storm
        if any(normalized_query and normalized_query in candidate for candidate in candidates):
            return storm
        if storm.storm_id.upper() in fallback_ids:
            return storm

    raise DapiyaFloaterError(f"Dapiya 当前未匹配到热带气旋：{text}")


def parse_dapiya_piclist(
    source: str,
    *,
    storm: DapiyaFloaterStorm,
    product: str,
) -> list[DapiyaFloaterFrame]:
    """Parse Dapiya comma-separated piclist response."""
    frames: list[DapiyaFloaterFrame] = []
    for item in source.strip().split(","):
        path = item.strip()
        if not path:
            continue
        url = urljoin(_DATA_BASE, path)
        frames.append(
            DapiyaFloaterFrame(
                storm_id=storm.storm_id,
                name=storm.name,
                product=product,
                url=url,
                time=_extract_frame_time(path),
            )
        )
    return frames


async def fetch_dapiya_floater(
    query: str = "",
    *,
    product: str = _DEFAULT_PRODUCT,
    frame_count: int = 42,
) -> DapiyaFloaterFrame:
    """Fetch the latest Dapiya floater frame for a storm query."""
    frames = await fetch_dapiya_floater_frames(
        query,
        product=product,
        frame_count=frame_count,
    )
    return frames[-1]


async def fetch_dapiya_floater_frames(
    query: str = "",
    *,
    product: str = _DEFAULT_PRODUCT,
    frame_count: int = 42,
) -> list[DapiyaFloaterFrame]:
    """Fetch Dapiya floater frames for a storm query, ordered oldest-first."""
    normalized_product = normalize_dapiya_product(product)
    async with httpx.AsyncClient() as client:
        active_res = await client.get(
            f"{_API_BASE}/typhoon/meso/all",
            timeout=15.0,
            follow_redirects=True,
        )
        active_res.raise_for_status()
        storm = resolve_dapiya_storm(parse_dapiya_active_storms(active_res.text), query)

        pic_res = await client.get(
            f"{_API_BASE}/typhoon/{storm.storm_id}/piclist/{normalized_product}/{frame_count}",
            timeout=15.0,
            follow_redirects=True,
        )
        pic_res.raise_for_status()

    frames = parse_dapiya_piclist(pic_res.text, storm=storm, product=normalized_product)
    if not frames:
        raise DapiyaFloaterError(f"Dapiya {storm.storm_id} {normalized_product} 暂无云图")
    return frames


async def download_dapiya_floater_image(url: str, dest: Path) -> None:
    """Download a Dapiya floater image to *dest*."""
    async with httpx.AsyncClient() as client:
        res = await client.get(url, timeout=30.0, follow_redirects=True)
        res.raise_for_status()
    dest.write_bytes(res.content)


async def fetch_dapiya_floater_gif(
    query: str = "",
    *,
    product: str = _DEFAULT_PRODUCT,
    max_frames: int = 24,
) -> tuple[bytes, DapiyaFloaterFrame, DapiyaFloaterFrame]:
    """Fetch Dapiya floater frames and render an animated GIF."""
    from shinbot_plugin_renderkit import GifRenderOptions, render_frames_to_gif

    frames = await fetch_dapiya_floater_frames(
        query,
        product=product,
        frame_count=max(1, max_frames),
    )
    frames = frames[-max_frames:]
    raw_frames = await _download_frames([frame.url for frame in frames])
    gif_bytes = await render_frames_to_gif(raw_frames, options=GifRenderOptions(fps=5))
    return gif_bytes, frames[-1], frames[0]


async def _download_frames(urls: list[str]) -> list[bytes]:
    sem = asyncio.Semaphore(6)

    async def _one(url: str) -> bytes:
        async with sem:
            async with httpx.AsyncClient() as client:
                res = await client.get(url, timeout=30.0, follow_redirects=True)
                res.raise_for_status()
                return res.content

    return list(await asyncio.gather(*(_one(url) for url in urls)))


def _storm_id_candidates(query: str) -> set[str]:
    candidates: set[str] = set()
    for match in re.finditer(r"\b([0-9]{2})([A-Z])\b", query.upper()):
        candidates.add(match.group(1) + match.group(2))
    for match in re.finditer(r"\b[0-9]{2}([0-9]{2})\s*号?\b", query):
        candidates.add(match.group(1) + "W")
    return candidates


def _extract_frame_time(path: str) -> str:
    match = re.search(r"_(\d{14})\.(?:png|jpg|jpeg)$", path, flags=re.IGNORECASE)
    if not match:
        return ""
    stamp = match.group(1)
    return (
        f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]} "
        f"{stamp[8:10]}:{stamp[10:12]}:{stamp[12:14]}"
    )


def _normalize_query(value: str) -> str:
    return re.sub(r"[\s\"'“”‘’，,、号-]+", "", value).casefold()
