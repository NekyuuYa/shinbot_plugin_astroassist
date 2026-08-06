"""Typhoon query models, NMC quick bulletin provider, and text formatting."""

from __future__ import annotations

import asyncio
import html
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urljoin

import httpx

_UNAVAILABLE_MESSAGE = "台风数据源暂未接入，暂时无法查询实时路径。"
_UNAVAILABLE_HINT = "后续接入数据源后，可使用 `!台风 list` 或 `!台风 <名称或编号>` 查询。"
_NMC_TYPHOON_NEWS_URL = "https://www.nmc.cn/publish/typhoon/typhoon_new.html"
_NMC_TYPHOON_TRACK_URL = "https://www.nmc.cn/publish/typhoon/probability-img2.html"
_NMC_TYPHOON_TRACK_SEED_URLS = tuple(
    f"https://www.nmc.cn/publish/typhoon/probability-img{index}.html"
    for index in range(1, 11)
)
_NMC_BASE_URL = "https://www.nmc.cn"
_NMC_NO_MATCH_MESSAGE = "NMC 当前台风快讯未匹配查询。"
_NMC_TYPHOON_JSON_LIST_URL = (
    "http://typhoon.nmc.cn/weatherservice/typhoon/jsons/list_default"
)
_NMC_TYPHOON_JSON_VIEW_URL = (
    "http://typhoon.nmc.cn/weatherservice/typhoon/jsons/view_{id}"
)
_NMC_LEVEL_NAMES = {
    "TD": "热带低压",
    "TS": "热带风暴",
    "STS": "强热带风暴",
    "TY": "台风",
    "STY": "强台风",
    "SuperTY": "超强台风",
}
_NMC_DIRECTION_CN = {
    "N": "北",
    "NNE": "北北东",
    "NE": "东北",
    "ENE": "东北东",
    "E": "东",
    "ESE": "东南东",
    "SE": "东南",
    "SSE": "东南南",
    "S": "南",
    "SSW": "西南南",
    "SW": "西南",
    "WSW": "西南西",
    "W": "西",
    "WNW": "西北西",
    "NW": "西北",
    "NNW": "北北西",
}
_NMC_CIRCLE_NAMES = {
    "30KTS": "七级风圈",
    "50KTS": "十级风圈",
    "64KTS": "十二级风圈",
}


@dataclass(slots=True, frozen=True)
class TyphoonSummary:
    """One active typhoon summary."""

    identifier: str
    name: str
    english_name: str = ""
    status: str = ""
    updated_at: str = ""
    center_lat: float | None = None
    center_lon: float | None = None
    wind_speed: float | None = None
    pressure: int | None = None
    source_id: str = ""


@dataclass(slots=True, frozen=True)
class TyphoonTrackPoint:
    """One observed or forecast typhoon track point."""

    time: str
    lat: float
    lon: float
    level: str = ""
    wind_speed: float | None = None
    pressure: int | None = None
    movement: str = ""


@dataclass(slots=True, frozen=True)
class TyphoonDetail:
    """Detailed typhoon state plus observed and forecast tracks."""

    summary: TyphoonSummary
    source: str = ""
    issue_number: str = ""
    issue_time: str = ""
    observation_time: str = ""
    reference_position: str = ""
    wind_circle: str = ""
    forecast_conclusion: str = ""
    track: list[TyphoonTrackPoint] = field(default_factory=list)
    forecast: list[TyphoonTrackPoint] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class TyphoonUnavailable:
    """Status returned when no realtime typhoon provider is connected."""

    message: str = _UNAVAILABLE_MESSAGE
    hint: str = _UNAVAILABLE_HINT


@dataclass(slots=True, frozen=True)
class TyphoonTrackImage:
    """One NMC typhoon path forecast image frame."""

    url: str
    time: str = ""
    name: str = ""


@dataclass(slots=True, frozen=True)
class TyphoonTrackPage:
    """One NMC typhoon path forecast page entry."""

    name: str
    url: str
    active: bool = False


type TyphoonListResult = Sequence[TyphoonSummary] | TyphoonUnavailable
type TyphoonDetailResult = TyphoonDetail | TyphoonUnavailable
type TyphoonTrackImageResult = TyphoonTrackImage | TyphoonUnavailable


class TyphoonProvider(Protocol):
    """Realtime typhoon data provider interface."""

    async def list_active(self) -> TyphoonListResult:
        """Return active typhoons or an unavailable status."""

    async def get_detail(self, query: str) -> TyphoonDetailResult:
        """Return detail for a typhoon name/identifier or an unavailable status."""

    async def get_track_image(self, query: str = "") -> TyphoonTrackImageResult:
        """Return latest path forecast image for a typhoon query."""


class UnavailableTyphoonProvider:
    """Default provider used until a realtime typhoon source is connected."""

    async def list_active(self) -> TyphoonUnavailable:
        """Return the framework placeholder status."""
        return TyphoonUnavailable()

    async def get_detail(self, query: str) -> TyphoonUnavailable:
        """Return the framework placeholder status."""
        return TyphoonUnavailable(
            hint=f"{_UNAVAILABLE_HINT}\n收到的查询：{query.strip() or '未指定'}"
        )

    async def get_track_image(self, query: str = "") -> TyphoonUnavailable:
        """Return the framework placeholder status."""
        return TyphoonUnavailable()


class _TrackQueryNotFound(ValueError):
    """Raised when a valid NMC track page navigation does not contain a query."""


class NmcTyphoonNewsProvider:
    """Provider for the latest NMC typhoon quick bulletin."""

    def __init__(
        self,
        *,
        url: str = _NMC_TYPHOON_NEWS_URL,
        track_url: str = _NMC_TYPHOON_TRACK_URL,
        track_urls: Sequence[str] | None = None,
        fetch_html: Callable[[], Awaitable[str]] | None = None,
        fetch_track_html: Callable[[str], Awaitable[str]] | None = None,
        json_list_url: str = _NMC_TYPHOON_JSON_LIST_URL,
        json_view_url: str = _NMC_TYPHOON_JSON_VIEW_URL,
        fetch_json: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._url = url
        self._track_url = track_url
        seed_urls = (
            list(track_urls)
            if track_urls is not None
            else [track_url, *_NMC_TYPHOON_TRACK_SEED_URLS]
        )
        self._track_urls = _dedupe_urls(seed_urls)
        self._fetch_html = fetch_html
        self._fetch_track_html = fetch_track_html
        self._json_list_url = json_list_url
        self._json_view_url = json_view_url
        self._fetch_json = fetch_json

    async def list_active(self) -> TyphoonListResult:
        """Return every active NMC typhoon, merging the latest bulletin's state."""
        try:
            summaries = await self._fetch_active_summaries()
        except Exception:
            summaries = []

        if not summaries:
            detail = await self._fetch_detail()
            if isinstance(detail, TyphoonUnavailable):
                return detail
            return [detail.summary]

        bulletin = await self._fetch_detail()
        if not isinstance(bulletin, TyphoonDetail):
            return summaries

        bulletin_summary = bulletin.summary
        merged: list[TyphoonSummary] = []
        for summary in summaries:
            if summary.identifier and summary.identifier == bulletin_summary.identifier:
                merged.append(
                    replace(
                        summary,
                        status=bulletin_summary.status,
                        updated_at=bulletin_summary.updated_at,
                        center_lat=bulletin_summary.center_lat,
                        center_lon=bulletin_summary.center_lon,
                        wind_speed=bulletin_summary.wind_speed,
                        pressure=bulletin_summary.pressure,
                    )
                )
            else:
                merged.append(summary)
        return merged

    async def get_detail(self, query: str) -> TyphoonDetailResult:
        """Return detail for *query* across every active NMC typhoon.

        The latest quick bulletin is served as-is; other active typhoons are
        resolved through the NMC typhoon JSON track service.
        """
        query_text = query.strip()
        bulletin_task = asyncio.create_task(self._fetch_detail())
        try:
            summaries = await self._fetch_active_summaries()
        except Exception:
            summaries = []
        bulletin = await bulletin_task

        if not summaries:
            summaries = [bulletin.summary] if isinstance(bulletin, TyphoonDetail) else []
        if not query_text:
            return bulletin

        matched = next((s for s in summaries if _matches_summary(s, query_text)), None)
        if matched is None:
            labels = "、".join(_summary_label(s) for s in summaries) if summaries else "暂无"
            return TyphoonUnavailable(
                message=_NMC_NO_MATCH_MESSAGE,
                hint=f"当前快讯：{labels}。使用 `!台风` 查看最新快讯。",
            )
        if (
            isinstance(bulletin, TyphoonDetail)
            and matched.identifier
            and bulletin.summary.identifier == matched.identifier
        ):
            return bulletin

        try:
            return await self._fetch_view_detail(matched)
        except Exception as exc:
            return TyphoonUnavailable(
                message="NMC 台风路径数据获取失败。",
                hint=f"{exc}",
            )

    async def get_track_image(self, query: str = "") -> TyphoonTrackImageResult:
        """Return latest NMC typhoon path forecast image when it matches *query*."""
        try:
            source = await self._track_source_for_query(query)
            images = parse_nmc_typhoon_track_images_html(source)
        except Exception as exc:
            return TyphoonUnavailable(
                message="NMC 台风路径预报图获取失败。",
                hint=f"{exc}",
            )

        if not images:
            return TyphoonUnavailable(
                message="NMC 台风路径预报图暂无数据。",
                hint="页面中未找到路径预报图片。",
            )

        return images[0]

    async def _fetch_detail(self) -> TyphoonDetailResult:
        try:
            source = await self._load_html()
            return parse_nmc_typhoon_news_html(source)
        except Exception as exc:
            return TyphoonUnavailable(
                message="NMC 台风快讯获取失败。",
                hint=f"{exc}",
            )

    async def _load_html(self) -> str:
        if self._fetch_html is not None:
            return await self._fetch_html()

        async with httpx.AsyncClient() as client:
            res = await client.get(self._url, timeout=15.0, follow_redirects=True)
            res.raise_for_status()
            return res.text

    async def _load_track_html(self, url: str) -> str:
        if self._fetch_track_html is not None:
            return await self._fetch_track_html(url)

        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0, follow_redirects=True)
            res.raise_for_status()
            return res.text

    async def _load_json(self, url: str) -> str:
        if self._fetch_json is not None:
            return await self._fetch_json(url)

        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0, follow_redirects=True)
            res.raise_for_status()
            return res.text

    async def _fetch_active_summaries(self) -> list[TyphoonSummary]:
        source = await self._load_json(self._json_list_url)
        return parse_nmc_typhoon_json_list(source)

    async def _fetch_view_detail(self, summary: TyphoonSummary) -> TyphoonDetail:
        if not summary.source_id:
            raise ValueError("缺少台风数据标识")
        source = await self._load_json(self._json_view_url.format(id=summary.source_id))
        return parse_nmc_typhoon_view_json(source)

    async def _track_source_for_query(self, query: str) -> str:
        query_text = query.strip()
        errors: list[str] = []
        for url in self._track_urls:
            try:
                source = await self._load_track_html(url)
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                continue

            if not query_text:
                if parse_nmc_typhoon_track_images_html(source):
                    return source
                errors.append(f"{url}: 页面中未找到路径预报图片")
                continue

            try:
                return await self._match_track_source_for_query(source, query_text, url)
            except _TrackQueryNotFound:
                raise
            except Exception as exc:
                errors.append(str(exc))

        if errors:
            raise ValueError("；".join(errors))
        raise ValueError("未配置台风路径预报入口")

    async def _match_track_source_for_query(
        self,
        source: str,
        query_text: str,
        source_url: str,
    ) -> str:
        has_track_nav = bool(_extract_track_nav_blocks(source))
        pages = parse_nmc_typhoon_track_pages_html(source, base_url=source_url)
        active_page = next((page for page in pages if page.active), None)
        active_name = (
            active_page.name if active_page is not None else _extract_track_page_name(source)
        )
        if _query_matches_name(query_text, active_name):
            return source

        for page in pages:
            if _query_matches_name(query_text, page.name):
                if page.url == source_url:
                    return source
                return await self._load_track_html(page.url)

        if pages:
            names = "、".join(page.name for page in pages if page.name)
            message = f"未找到与“{query_text}”匹配的路径预报页。当前可用：{names}"
            if has_track_nav:
                raise _TrackQueryNotFound(message)
            raise ValueError(message)
        raise ValueError("页面中未找到台风路径预报页列表")


@dataclass(slots=True, frozen=True)
class TyphoonCommand:
    """Parsed typhoon command."""

    action: Literal["help", "list", "detail"]
    query: str = ""


def parse_typhoon_args(raw: str) -> TyphoonCommand:
    """Parse typhoon command arguments into an action and optional query."""
    text = raw.strip()
    if not text:
        return TyphoonCommand(action="list")

    first, *_rest = text.split(maxsplit=1)
    token = first.casefold()
    if token in {"help", "帮助", "-h", "--help"}:
        return TyphoonCommand(action="help")
    if token in {"list", "ls", "active", "活跃", "列表"}:
        return TyphoonCommand(action="list")
    return TyphoonCommand(action="detail", query=text)


def format_typhoon_help() -> str:
    """Return user-facing typhoon command help."""
    return (
        "🌀 台风路径查询 | 指南\n"
        "━━━━━━━━━━━━━━━\n"
        "!台风 → 查询中央气象台最新台风快讯\n"
        "!台风 list → 列出当前活跃台风\n"
        "!台风 <名称或编号> → 查询任一活跃台风详情，并附带路径图与最新海区云图\n"
        "!台风云图 [名称或编号] [VIS|RGB|TRUECOLOR] → Dapiya 云图，默认 VIS\n"
        "!台风云图动图 [名称或编号] [VIS|RGB|TRUECOLOR] → Dapiya 云图动画\n"
        "!typhoon <name-or-id> → 英文别名\n\n"
        "数据源：中央气象台 NMC 台风快讯；Dapiya 热带气旋云图。"
    )


def format_typhoon_unavailable(status: TyphoonUnavailable) -> str:
    """Format an unavailable provider status."""
    return f"🌀 {status.message}\n{status.hint}"


def format_typhoon_list(result: TyphoonListResult) -> str:
    """Format an active typhoon list result."""
    if isinstance(result, TyphoonUnavailable):
        return format_typhoon_unavailable(result)
    if not result:
        return "🌀 当前没有活跃台风。"

    lines = ["🌀 NMC 最新台风快讯"]
    for item in result:
        label = _summary_label(item)
        status = f" · {item.status}" if item.status else ""
        updated = f" · 更新 {item.updated_at}" if item.updated_at else ""
        lines.append(f"- {label}{status}{updated}")
    lines.append("使用 `!台风 <名称或编号>` 查看详情。")
    return "\n".join(lines)


def format_typhoon_detail(result: TyphoonDetailResult) -> str:
    """Format a typhoon detail result."""
    if isinstance(result, TyphoonUnavailable):
        return format_typhoon_unavailable(result)

    summary = result.summary
    lines = [f"🌀 {_summary_label(summary)}"]
    if result.issue_number:
        lines.append(f"期号：{result.issue_number}")
    if result.source or result.issue_time:
        release = " · ".join(part for part in (result.source, result.issue_time) if part)
        lines.append(f"发布：{release}")
    if result.observation_time:
        lines.append(f"观测时间：{result.observation_time}")
    if summary.status:
        lines.append(f"状态：{summary.status}")
    if summary.updated_at:
        lines.append(f"更新时间：{summary.updated_at}")
    if summary.center_lat is not None and summary.center_lon is not None:
        lines.append(f"中心位置：{summary.center_lat:.1f}, {summary.center_lon:.1f}")
    if summary.wind_speed is not None:
        lines.append(f"近中心最大风速：{summary.wind_speed:g} m/s")
    if summary.pressure is not None:
        lines.append(f"中心气压：{summary.pressure} hPa")
    if result.reference_position:
        lines.append(f"参考位置：{result.reference_position}")
    if result.wind_circle:
        lines.append("风圈半径：")
        lines.extend(f"- {item}" for item in result.wind_circle.splitlines() if item.strip())
    if result.forecast_conclusion:
        lines.append(f"预报结论：{result.forecast_conclusion}")

    if result.track:
        lines.append("最近路径：")
        for point in result.track[-3:]:
            lines.append(f"- {_format_track_point(point)}")
    if result.forecast:
        lines.append("路径预报：")
        for point in result.forecast[:3]:
            lines.append(f"- {_format_track_point(point)}")

    return "\n".join(lines)


def parse_nmc_typhoon_news_html(source: str) -> TyphoonDetail:
    """Parse the latest NMC typhoon quick bulletin HTML."""
    fields = _extract_nmc_fields(source)
    name, english_name = _parse_typhoon_name(fields.get("命名", ""))
    identifier = _parse_identifier(fields.get("编号", ""))
    if not name and not identifier:
        raise ValueError("页面中未找到有效台风快讯")

    lat, lon = _parse_center_position(fields.get("中心位置", ""))
    wind_speed = _parse_wind_speed(fields.get("最大风力", ""))
    pressure = _parse_pressure(fields.get("中心气压", ""))
    issue_number = _extract_first(
        source,
        rf"<div[^>]+{_class_attr_pattern('number')}[^>]*>(.*?)</div>",
    )
    source_name, issue_time = _extract_ctitle(source)

    summary = TyphoonSummary(
        identifier=identifier,
        name=name,
        english_name=english_name,
        status=fields.get("强度等级", ""),
        updated_at=issue_time,
        center_lat=lat,
        center_lon=lon,
        wind_speed=wind_speed,
        pressure=pressure,
    )
    return TyphoonDetail(
        summary=summary,
        source=source_name,
        issue_number=issue_number,
        issue_time=issue_time,
        observation_time=fields.get("时间", ""),
        reference_position=fields.get("参考位置", ""),
        wind_circle=fields.get("风圈半径", ""),
        forecast_conclusion=fields.get("预报结论", ""),
    )


def parse_nmc_typhoon_json_list(source: str) -> list[TyphoonSummary]:
    """Parse the NMC typhoon index JSONP into active storm summaries."""
    data = _parse_jsonp(source)
    entries = data.get("typhoonList") or []
    summaries: list[TyphoonSummary] = []
    for entry in entries:
        if not isinstance(entry, list) or len(entry) < 8:
            continue
        if str(entry[7] or "").strip() != "start":
            continue
        english = str(entry[1] or "").strip()
        name = str(entry[2] or "").strip()
        number = _number_text(entry[3])
        if not name and not english:
            continue
        summaries.append(
            TyphoonSummary(
                identifier=number,
                name=name,
                english_name=english,
                source_id=str(entry[0]),
            )
        )
    return summaries


def parse_nmc_typhoon_view_json(source: str) -> TyphoonDetail:
    """Parse one NMC typhoon track JSONP response into a detail model."""
    data = _parse_jsonp(source)
    typhoon = data.get("typhoon")
    if not isinstance(typhoon, list) or len(typhoon) < 9:
        raise ValueError("页面中未找到有效台风路径数据")

    points = typhoon[8] or []
    track: list[TyphoonTrackPoint] = []
    forecast: list[TyphoonTrackPoint] = []
    level = ""
    center_lat = center_lon = None
    wind_speed: float | None = None
    pressure: int | None = None
    observation_time = ""
    updated_at = ""
    wind_circle = ""

    for point in points:
        parsed = _parse_view_point(point)
        if parsed is None:
            continue
        track.append(parsed)
        if point is points[-1]:
            level = parsed.level
            center_lat, center_lon = parsed.lat, parsed.lon
            wind_speed, pressure = parsed.wind_speed, parsed.pressure
            observation_time = parsed.time
            updated_at = _view_point_bulletin_time(point) or parsed.time
            wind_circle = _format_view_circles(_view_point_circles(point))
            forecast = _parse_view_forecast(point)

    summary = TyphoonSummary(
        identifier=_number_text(typhoon[3]),
        name=str(typhoon[2] or "").strip(),
        english_name=str(typhoon[1] or "").strip(),
        status=level,
        updated_at=updated_at,
        center_lat=center_lat,
        center_lon=center_lon,
        wind_speed=wind_speed,
        pressure=pressure,
        source_id=str(typhoon[0]),
    )
    return TyphoonDetail(
        summary=summary,
        source="中央气象台台风网",
        observation_time=observation_time,
        wind_circle=wind_circle,
        track=track,
        forecast=forecast,
    )


def _parse_jsonp(source: str) -> dict[str, Any]:
    text = source.strip()
    start = text.find("(")
    end = text.rfind(")")
    if start < 0 or end <= start:
        raise ValueError("无效的 NMC 台风 JSON 响应")
    payload_text = text[start + 1 : end].strip()
    # Some NMC endpoints wrap the object in an extra pair of parentheses.
    while payload_text.startswith("(") and payload_text.endswith(")"):
        payload_text = payload_text[1:-1].strip()
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ValueError("无效的 NMC 台风 JSON 响应") from exc
    if not isinstance(payload, dict):
        raise ValueError("无效的 NMC 台风 JSON 响应")
    return payload


def _number_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _parse_view_point(point: Any) -> TyphoonTrackPoint | None:
    if not isinstance(point, list) or len(point) < 10:
        return None
    stamp = str(point[1] or "")
    if len(stamp) != 12 or not stamp.isdigit():
        return None
    try:
        lon = float(point[4])
        lat = float(point[5])
    except (TypeError, ValueError):
        return None
    direction = str(point[8] or "")
    speed = _number_or_none(point[9])
    movement = ""
    if direction and direction != "0":
        movement = _NMC_DIRECTION_CN.get(direction, direction)
        if speed is not None:
            movement = f"{movement} {speed:g}公里/小时"
    return TyphoonTrackPoint(
        time=_format_view_stamp(stamp),
        lat=lat,
        lon=lon,
        level=_NMC_LEVEL_NAMES.get(str(point[3] or ""), str(point[3] or "")),
        wind_speed=_number_or_none(point[7]),
        pressure=_int_or_none(point[6]),
        movement=movement,
    )


def _parse_view_forecast(point: Any) -> list[TyphoonTrackPoint]:
    forecasts = point[11] if len(point) > 11 and isinstance(point[11], dict) else {}
    entries = forecasts.get("BABJ") or []
    base_stamp = str(point[1] or "")
    try:
        base_time = datetime.strptime(base_stamp, "%Y%m%d%H%M")
    except ValueError:
        base_time = None

    result: list[TyphoonTrackPoint] = []
    for entry in entries:
        if not isinstance(entry, list) or len(entry) < 8:
            continue
        hours = entry[0]
        time_label = f"+{hours}h"
        if base_time is not None and isinstance(hours, (int, float)):
            time_label = (base_time + timedelta(hours=int(hours))).strftime("%m/%d %H:%M")
        result.append(
            TyphoonTrackPoint(
                time=time_label,
                lat=_number_or_none(entry[3]) or 0,
                lon=_number_or_none(entry[2]) or 0,
                level=_NMC_LEVEL_NAMES.get(str(entry[7] or ""), str(entry[7] or "")),
                wind_speed=_number_or_none(entry[5]),
                pressure=_int_or_none(entry[4]),
            )
        )
    return result


def _format_view_circles(circles: Any) -> str:
    if not circles:
        return ""
    lines: list[str] = []
    for circle in circles:
        if not isinstance(circle, list) or len(circle) < 5:
            continue
        code = str(circle[0] or "")
        label = _NMC_CIRCLE_NAMES.get(code, f"{code}风圈")
        lines.append(
            f"{label}半径 东北方向{circle[1]}公里；东南方向{circle[2]}公里；"
            f"西南方向{circle[3]}公里；西北方向{circle[4]}公里"
        )
    return "\n".join(lines)


def _view_point_circles(point: Any) -> list[Any]:
    if len(point) > 10 and isinstance(point[10], list):
        return point[10]
    return []


def _view_point_bulletin_time(point: Any) -> str:
    if len(point) > 12 and isinstance(point[12], list) and len(point[12]) > 1:
        value = point[12][1]
        return str(value).strip() if value else ""
    return ""


def _format_view_stamp(stamp: str) -> str:
    return f"{stamp[4:6]}/{stamp[6:8]} {stamp[8:10]}:{stamp[10:12]}"


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_nmc_typhoon_track_pages_html(
    source: str,
    *,
    base_url: str = _NMC_TYPHOON_TRACK_URL,
) -> list[TyphoonTrackPage]:
    """Parse available NMC typhoon path forecast pages from the navigation."""
    pages: list[TyphoonTrackPage] = []
    seen: set[str] = set()
    search_sources = _extract_track_nav_blocks(source) or [source]
    for nav_source in search_sources:
        for match in re.finditer(
            r"<a\b[^>]+href\s*=\s*(\"[^\"]*probability-img\d+\.html\"|'[^']*probability-img\d+\.html'|[^\s>]*probability-img\d+\.html)[^>]*>.*?</a>",
            nav_source,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            tag = match.group(0)
            attrs = _parse_attrs(tag)
            href = attrs.get("href", "").strip()
            if not href:
                continue
            url = urljoin(base_url or _NMC_BASE_URL, href)
            if url in seen:
                continue
            seen.add(url)
            pages.append(
                TyphoonTrackPage(
                    name=_clean_html_text(tag),
                    url=url,
                    active="actived" in _class_names(attrs.get("class", "")),
                )
            )
    return pages


def parse_nmc_typhoon_track_images_html(source: str) -> list[TyphoonTrackImage]:
    """Parse NMC typhoon path forecast image frames from probability image page."""
    name = _extract_track_page_name(source)
    frames: list[TyphoonTrackImage] = []
    seen: set[str] = set()
    for attrs in _iter_attrs(source, "div"):
        if "time" not in _class_names(attrs.get("class", "")):
            continue
        url = attrs.get("data-img", "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        frames.append(
            TyphoonTrackImage(
                url=url,
                time=attrs.get("data-time", "").strip(),
                name=name,
            )
        )

    if not frames:
        img_attrs = _extract_imgpath_attrs(source)
        url = img_attrs.get("src", "").strip()
        if url:
            frames.append(
                TyphoonTrackImage(
                    url=url,
                    time=img_attrs.get("data-time", "").strip(),
                    name=name,
                )
            )
    return frames


async def download_typhoon_track_image(url: str, dest: str) -> None:
    """Download a typhoon path forecast image to *dest*."""
    async with httpx.AsyncClient() as client:
        res = await client.get(url, timeout=30.0, follow_redirects=True)
        res.raise_for_status()

    Path(dest).write_bytes(res.content)


def _summary_label(summary: TyphoonSummary) -> str:
    name = summary.name
    if summary.english_name:
        name = f"{name} ({summary.english_name})"
    if summary.identifier:
        return f"{summary.identifier} {name}"
    return name


def _matches_summary(summary: TyphoonSummary, query: str) -> bool:
    target = _normalize_query(query)
    candidates = [
        summary.identifier,
        summary.name,
        summary.english_name,
        _summary_label(summary),
        f"{summary.identifier}号",
        f"{summary.identifier} 号",
    ]
    return any(target and target in _normalize_query(candidate) for candidate in candidates)


def _query_matches_name(query: str, name: str) -> bool:
    normalized_query = _normalize_query(query)
    normalized_name = _normalize_query(name)
    return bool(normalized_query and normalized_name and normalized_query in normalized_name)


def _format_track_point(point: TyphoonTrackPoint) -> str:
    parts = [f"{point.time} {point.lat:.1f}, {point.lon:.1f}"]
    if point.level:
        parts.append(point.level)
    if point.wind_speed is not None:
        parts.append(f"{point.wind_speed:g} m/s")
    if point.pressure is not None:
        parts.append(f"{point.pressure} hPa")
    if point.movement:
        parts.append(point.movement)
    return " · ".join(parts)


def _extract_nmc_fields(source: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for label_html, value_html in re.findall(
        r"<tr\b[^>]*>\s*<td\b[^>]*>(.*?)</td>\s*<td\b[^>]*>(.*?)</td>\s*</tr>",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        label = _normalize_label(_clean_html_text(label_html))
        if not label:
            continue
        fields[label] = _clean_html_text(value_html, preserve_breaks=True)
    return fields


def _extract_track_page_name(source: str) -> str:
    pages = parse_nmc_typhoon_track_pages_html(source)
    active_page = next((page for page in pages if page.active and page.name), None)
    if active_page is not None:
        return active_page.name

    title = _extract_first(source, r"<title>(.*?)</title>")
    if "_" in title:
        return title.rsplit("_", 1)[-1].strip()
    return title


def _extract_track_nav_blocks(source: str) -> list[str]:
    blocks: list[str] = []
    for match in re.finditer(
        rf"<div\b[^>]+{_class_attr_pattern('p-nav')}[^>]*>.*?</div>\s*</div>",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        block = match.group(0)
        if "probability-img" in block:
            blocks.append(block)
    return blocks


def _dedupe_urls(urls: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def _extract_imgpath_attrs(source: str) -> dict[str, str]:
    match = re.search(r"<img\b[^>]*\bid\s*=\s*[\"']?imgpath[\"']?[^>]*>", source, re.IGNORECASE)
    if not match:
        return {}
    return _parse_attrs(match.group(0))


def _iter_attrs(source: str, tag: str) -> list[dict[str, str]]:
    return [
        _parse_attrs(match.group(0))
        for match in re.finditer(rf"<{tag}\b[^>]*>", source, flags=re.IGNORECASE | re.DOTALL)
    ]


def _parse_attrs(tag_source: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in re.finditer(
        r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)",
        tag_source,
        flags=re.IGNORECASE,
    ):
        raw = match.group(2).strip()
        quoted = (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")
        )
        if quoted:
            raw = raw[1:-1]
        attrs[match.group(1).lower()] = html.unescape(raw)
    return attrs


def _class_names(value: str) -> set[str]:
    return {item for item in re.split(r"\s+", value.strip()) if item}


def _extract_ctitle(source: str) -> tuple[str, str]:
    block_match = re.search(
        rf"<div[^>]+{_class_attr_pattern('ctitle')}[^>]*>(.*?)(?:<hr\b|<table\b)",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    block = block_match.group(1) if block_match else ""
    if not block:
        return "", ""
    spans = re.findall(r"<span\b[^>]*>(.*?)</span>", block, flags=re.IGNORECASE | re.DOTALL)
    values = [_clean_html_text(item) for item in spans]
    source_name = values[0] if values else ""
    issue_time = values[1] if len(values) > 1 else ""
    return source_name, issue_time


def _extract_first(source: str, pattern: str) -> str:
    match = re.search(pattern, source, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return _clean_html_text(match.group(1))


def _class_attr_pattern(class_name: str) -> str:
    escaped = re.escape(class_name)
    return (
        r"class\s*=\s*(?:"
        rf'"[^"]*\b{escaped}\b[^"]*"'
        r"|"
        rf"'[^']*\b{escaped}\b[^']*'"
        r"|"
        rf"[^\s>]*\b{escaped}\b[^\s>]*"
        r")"
    )


def _clean_html_text(source: str, *, preserve_breaks: bool = False) -> str:
    text = re.sub(r"<br\s*/?>", "\n", source, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    if preserve_breaks:
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_label(label: str) -> str:
    return re.sub(r"\s+", "", label).strip(":：")


def _normalize_query(value: str) -> str:
    return re.sub(r"[\s\"'“”‘’，,、号-]+", "", value).casefold()


def _parse_typhoon_name(value: str) -> tuple[str, str]:
    match = re.search(
        r"[“\"](?P<name>[^”\"]+)[”\"]\s*[，,、]?\s*(?P<en>[A-Za-z][A-Za-z0-9 -]*)?",
        value,
    )
    if match:
        return match.group("name").strip(), (match.group("en") or "").strip()

    parts = [part.strip(" “”\"") for part in re.split(r"[，,、/]", value) if part.strip()]
    if not parts:
        return "", ""
    english_name = parts[1] if len(parts) > 1 and re.search(r"[A-Za-z]", parts[1]) else ""
    return parts[0], english_name


def _parse_identifier(value: str) -> str:
    match = re.search(r"(\d{2,4})\s*号?", value)
    return match.group(1) if match else ""


def _parse_center_position(value: str) -> tuple[float | None, float | None]:
    match = re.search(
        r"(北纬|南纬)\s*([0-9.]+)\s*度[、,，\s]*(东经|西经)\s*([0-9.]+)\s*度",
        value,
    )
    if not match:
        return None, None
    lat = float(match.group(2))
    lon = float(match.group(4))
    if match.group(1) == "南纬":
        lat = -lat
    if match.group(3) == "西经":
        lon = -lon
    return lat, lon


def _parse_wind_speed(value: str) -> float | None:
    match = re.search(r"([0-9.]+)\s*米/秒", value)
    return float(match.group(1)) if match else None


def _parse_pressure(value: str) -> int | None:
    match = re.search(r"(\d+)\s*hPa", value, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None
