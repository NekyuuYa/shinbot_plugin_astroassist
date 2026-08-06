"""DarkMap sky brightness (SQM) fetching and Bortle classification."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

_LATEST_URL = "https://www.darkmap.cn/api/lightpollution/latest"
_SKY_PROFILE_URL = "https://www.darkmap.cn/api/lightpollution/sky-profile"
# DarkMap serves yearly baselines for this range; others are clamped.
_MIN_YEAR = 2012
_MAX_YEAR = 2025

# Bortle class upper brightness thresholds in mag/arcsec² (SQM). Standard
# published table (Wikipedia "Bortle scale" approximations): a sky at or
# above a threshold belongs to that class, anything below 17.80 is class 9.
_BORTLE_THRESHOLDS = (
    (21.75, 1),
    (21.60, 2),
    (21.30, 3),
    (20.49, 4),
    (19.50, 5),
    (18.94, 6),
    (18.38, 7),
    (17.80, 8),
)


@dataclass(frozen=True, slots=True)
class BortleInfo:
    """Descriptive profile of one Bortle class."""

    label: str
    nelm: str
    milky_way: str
    advice: str


_BORTLE_INFO: dict[int, BortleInfo] = {
    1: BortleInfo(
        label="极佳暗夜",
        nelm="7.6–8.0",
        milky_way="银河极其明显，黄道光与气辉清晰可见",
        advice="顶级暗夜，极适合深空摄影与目视",
    ),
    2: BortleInfo(
        label="典型暗夜",
        nelm="7.1–7.5",
        milky_way="银河结构清晰，黄道光可见",
        advice="适合深空摄影与目视观测",
    ),
    3: BortleInfo(
        label="乡村夜空",
        nelm="6.6–7.0",
        milky_way="银河可见且有明显结构，部分暗天体需侧视",
        advice="适合多数天文观测",
    ),
    4: BortleInfo(
        label="乡村/城郊过渡",
        nelm="6.1–6.5",
        milky_way="银河可见但细节减少，暗星云难以辨认",
        advice="适合行星/双星观测，深空观测受限",
    ),
    5: BortleInfo(
        label="郊区",
        nelm="5.6–6.0",
        milky_way="银河仅在头顶方向隐约可见",
        advice="适合行星观测与较亮深空天体",
    ),
    6: BortleInfo(
        label="明亮郊区",
        nelm="5.1–5.5",
        milky_way="银河仅在天顶附近可见",
        advice="适合月球与行星观测",
    ),
    7: BortleInfo(
        label="城郊过渡",
        nelm="4.6–5.0",
        milky_way="银河几乎不可见",
        advice="仅适合月球与亮行星观测",
    ),
    8: BortleInfo(
        label="城市",
        nelm="4.1–4.5",
        milky_way="银河不可见，仅见亮星",
        advice="适合月球与大行星目视",
    ),
    9: BortleInfo(
        label="市中心",
        nelm="≤4.0",
        milky_way="仅可见最亮恒星与行星",
        advice="基本不适合天文观测",
    ),
}


@dataclass(frozen=True, slots=True)
class LightPollution:
    """Sky brightness at one location from the DarkMap service."""

    mpsas: float
    ratio: float
    bortle: int
    label: str
    data_version: str = ""
    year: int | None = None

    @property
    def info(self) -> BortleInfo:
        """The descriptive profile of this sky's Bortle class."""
        return _BORTLE_INFO[self.bortle]

    @property
    def bortle_text(self) -> str:
        """Compact user-facing summary, e.g. ``Bortle 4 · 乡村/城郊过渡``."""
        return f"Bortle {self.bortle} · {self.label}"


def classify_bortle(mpsas: float) -> int:
    """Map sky brightness in mag/arcsec² to the 1-9 Bortle class."""
    for threshold, bortle in _BORTLE_THRESHOLDS:
        if mpsas >= threshold:
            return bortle
    return 9


def _validate_coordinates(lat: float, lon: float) -> None:
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise ValueError(f"无效坐标: ({lat}, {lon})")


async def fetch_light_pollution(
    lat: float,
    lon: float,
    year: int | None = None,
) -> LightPollution:
    """Fetch sky brightness at *(lat, lon)* from DarkMap.

    With *year* set the yearly ``sky-profile`` endpoint is used (2012-2025,
    clamped); otherwise the latest baseline is returned.
    """
    _validate_coordinates(lat, lon)
    params: dict[str, float | int] = {"lat": lat, "lon": lon}
    url = _LATEST_URL
    if year is not None:
        url = _SKY_PROFILE_URL
        params["year"] = max(_MIN_YEAR, min(_MAX_YEAR, int(year)))

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        res = await client.get(url, params=params)
        res.raise_for_status()
        payload = res.json()

    if "brightness" in payload:
        try:
            mpsas = float(payload["brightness"]["mpsas"])
            ratio = float(payload["brightness"]["ratio"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("DarkMap 响应缺少亮度数据") from exc
        data_version = str(payload.get("dataVersion") or "").strip()
        returned_year = None
    elif "query_point" in payload:
        try:
            mpsas = float(payload["query_point"]["sqm"])
            ratio = float(payload["query_point"]["lpi"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("DarkMap 响应缺少亮度数据") from exc
        data_version = ""
        try:
            returned_year = int(payload["year"])
        except (KeyError, TypeError, ValueError):
            returned_year = None
    else:
        raise ValueError("DarkMap 响应缺少亮度数据")

    if not (0.0 <= mpsas <= 24.0) or ratio < 0:
        raise ValueError("DarkMap 返回了无效的亮度数据")

    bortle = classify_bortle(mpsas)
    return LightPollution(
        mpsas=mpsas,
        ratio=ratio,
        bortle=bortle,
        label=_BORTLE_INFO[bortle].label,
        data_version=data_version,
        year=returned_year,
    )


def format_light_pollution_report(
    location_name: str,
    lat: float,
    lon: float,
    light_pollution: LightPollution,
) -> str:
    """Format a user-facing light pollution report for *location_name*."""
    info = light_pollution.info
    lines = [
        f"🌌 光污染报告 | {location_name or '当前位置'}",
        f"📍 {lat:.4f}, {lon:.4f}",
        f"Bortle {light_pollution.bortle} · {info.label}",
        "━━━━━━━━━━━━━━━",
        f"天光背景 (SQM)：{light_pollution.mpsas:.2f} mag/arcsec²",
        f"相对自然天光：{light_pollution.ratio:.2f} ×",
        f"极限星等 (NELM)：约 {info.nelm} 等",
        f"银河可见性：{info.milky_way}",
        f"观测建议：{info.advice}",
    ]
    if light_pollution.year is not None:
        lines.append(f"数据年份：{light_pollution.year}")
    if light_pollution.data_version:
        lines.append(f"数据源：DarkMap {light_pollution.data_version}")
    return "\n".join(lines)
