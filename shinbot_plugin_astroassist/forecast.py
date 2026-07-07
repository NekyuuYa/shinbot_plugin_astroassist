"""Fetch and process forecast data from Open-Meteo (ECMWF) and 7Timer!."""

from __future__ import annotations

import asyncio
import datetime
from typing import Any

import httpx

from .models import RenderData

# ------------------------------------------------------------------
# Color mapping helpers
# ------------------------------------------------------------------

_DAY_COLORS: dict[str, dict[str, tuple[str, str]]] = {
    "temp": {
        "frigid":    ("#003258", "on-dark"),   # < -10
        "cold":      ("#D1E4FF", "on-light"),  # ~ 0
        "cool":      ("#C4E7CB", "on-light"),  # ~ 8
        "mild":      ("#A8C7FF", "on-light"),  # ~ 16
        "warm":      ("#E8F0FF", "on-light"),  # ~ 24
        "hot":       ("#FFECB3", "on-light"),  # ~ 30
        "vhot":      ("#FFDAD6", "on-light"),  # ~ 36
        "extreme":   ("#BA1A1A", "on-dark"),   # ~ 38
        "scorching": ("#4A148C", "on-dark"),   # > 38
    },
    "seeing_trans": {
        "excellent": ("#10b981", "on-dark"),   # 1-2
        "good":      ("#3b82f6", "on-dark"),   # 3-4
        "moderate":  ("#f59e0b", "on-light"),  # 5-6
        "poor":      ("#BA1A1A", "on-dark"),   # 7-8
        "na":        ("#E5E7EB", "on-light"),  # /
    },
    "humi": {
        "dry":       ("#C4E7CB", "on-light"),  # < 40
        "normal":    ("#A8C7FF", "on-light"),  # ~ 70
        "humid":     ("#FFECB3", "on-light"),  # ~ 90
        "wet":       ("#BA1A1A", "on-dark"),   # > 90
    },
}

_NIGHT_OVERRIDES: dict[str, dict[str, tuple[str, str]]] = {
    "temp": {
        "frigid":    ("#001D3D", "on-dark"),
        "cold":      ("#003258", "on-dark"),
        "cool":      ("#064E3B", "on-dark"),
        "mild":      ("#003566", "on-dark"),
        "warm":      ("#1E293B", "on-dark"),
        "hot":       ("#78350F", "on-dark"),
        "vhot":      ("#7F1D1D", "on-dark"),
        "extreme":   ("#450A0A", "on-dark"),
        "scorching": ("#1A0533", "on-dark"),
    },
    "seeing_trans": {
        "excellent": ("#064E3B", "on-dark"),
        "good":      ("#1E3A8A", "on-dark"),
        "moderate":  ("#78350F", "on-light"),
        "poor":      ("#450A0A", "on-dark"),
        "na":        ("#2A2A2A", "on-dark"),
    },
    "humi": {
        "dry":       ("#064E3B", "on-dark"),
        "normal":    ("#1E3A8A", "on-dark"),
        "humid":     ("#78350F", "on-dark"),
        "wet":       ("#450A0A", "on-dark"),
    },
}


def _temp_bucket(v: float) -> str:
    if v < -10:
        return "frigid"
    if v <= 0:
        return "cold"
    if v <= 8:
        return "cool"
    if v <= 16:
        return "mild"
    if v <= 24:
        return "warm"
    if v <= 30:
        return "hot"
    if v <= 36:
        return "vhot"
    if v <= 38:
        return "extreme"
    return "scorching"


def _seeing_trans_bucket(v: int | str) -> str:
    if isinstance(v, str):
        return "na"
    if v <= 2:
        return "excellent"
    if v <= 4:
        return "good"
    if v <= 6:
        return "moderate"
    return "poor"


def _humi_bucket(v: float) -> str:
    if v < 40:
        return "dry"
    if v < 70:
        return "normal"
    if v < 90:
        return "humid"
    return "wet"


def _color(category: str, bucket: str, is_night: bool) -> tuple[str, str]:
    """Return ``(hex_color, css_class)`` for the given bucket."""
    table = _NIGHT_OVERRIDES if is_night else _DAY_COLORS
    return table[category][bucket]


def get_m3_color(v: float | int | str, type_: str, *, is_night: bool) -> tuple[str, str]:
    """Public color mapping used by the template pipeline."""
    if type_ == "temp":
        return _color("temp", _temp_bucket(float(v)), is_night)
    if type_ in ("seeing", "trans"):
        sv = v if isinstance(v, (int, str)) else int(v)
        return _color("seeing_trans", _seeing_trans_bucket(sv), is_night)
    if type_ == "humi":
        return _color("humi", _humi_bucket(float(v)), is_night)
    # Wind reuses humidity buckets (same thresholds)
    if type_ == "wind":
        return _color("humi", _humi_bucket(float(v)), is_night)
    return ("#E5E7EB", "on-light")


def _dew_color(dew: float, *, is_night: bool) -> tuple[str, str]:
    """Dew point risk coloring (lower dew → higher condensation risk)."""
    if dew < 2:
        c = "#450A0A" if is_night else "#ef4444"
        return c, "on-dark"
    if dew <= 5:
        c = "#78350F" if is_night else "#f59e0b"
        return c, ("on-dark" if is_night else "on-light")
    c = "#064E3B" if is_night else "#10b981"
    return c, ("on-dark" if is_night else "on-light")


# ------------------------------------------------------------------
# Data fetching
# ------------------------------------------------------------------

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_SEVEN_TIMER_URL = "https://www.7timer.info/bin/astro.php"


async def _fetch_open_meteo(lat: float, lon: float, days: int) -> dict[str, Any]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": (
            "cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
            "temperature_2m,relative_humidity_2m,dew_point_2m,wind_speed_10m"
        ),
        "daily": "sunrise,sunset",
        "models": "ecmwf_ifs025",
        "forecast_days": days,
        "timezone": "auto",
    }
    async with httpx.AsyncClient() as client:
        res = await client.get(_OPEN_METEO_URL, params=params, timeout=10.0)
        res.raise_for_status()
        return res.json()


async def _fetch_7timer(lat: float, lon: float) -> dict[str, Any]:
    params = {"lon": lon, "lat": lat, "ac": 0, "unit": "metric", "output": "json", "tzshift": 0}
    async with httpx.AsyncClient() as client:
        res = await client.get(_SEVEN_TIMER_URL, params=params, timeout=10.0)
        if res.status_code == 200 and "dataseries" in res.text:
            return res.json()
    return {}


def _build_timer_map(t_data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Map UTC timestamps → 7Timer! dataseries entries."""
    if not t_data.get("init"):
        return {}
    init_dt = datetime.datetime.strptime(t_data["init"], "%Y%m%d%H").replace(
        tzinfo=datetime.UTC
    )
    result: dict[int, dict[str, Any]] = {}
    for entry in t_data.get("dataseries", []):
        ts = int((init_dt + datetime.timedelta(hours=entry["timepoint"])).timestamp())
        result[ts] = entry
    return result


def _find_timer_match(timer_map: dict[int, dict[str, Any]], utc_ts: int) -> dict[str, Any] | None:
    """Nearest-neighbour match within ±2 h."""
    best: dict[str, Any] | None = None
    best_d = 999999
    for t_ts, val in timer_map.items():
        d = abs(utc_ts - t_ts)
        if d < best_d and d <= 7200:
            best_d = d
            best = val
    return best


# ------------------------------------------------------------------
# Processing
# ------------------------------------------------------------------


async def fetch_forecast(
    lat: float,
    lon: float,
    *,
    days: int = 3,
    night_only: bool = False,
) -> RenderData:
    """Fetch and process all data, returning a ready-to-render bundle."""
    m_data, t_data = await asyncio.gather(
        _fetch_open_meteo(lat, lon, days),
        _fetch_7timer(lat, lon),
    )

    hourly = m_data["hourly"]
    daily = m_data["daily"]
    now = datetime.datetime.now()

    # Theme mode: compare local (naive) now against sunrise/sunset
    sunrise_0 = datetime.datetime.fromisoformat(daily["sunrise"][0])
    sunset_0 = datetime.datetime.fromisoformat(daily["sunset"][0])
    is_night = now.replace(tzinfo=None) < sunrise_0.replace(tzinfo=None) or \
               now.replace(tzinfo=None) > sunset_0.replace(tzinfo=None)
    theme_mode = "night-mode" if is_night else "light-mode"

    # Build sunrise/sunset transition rows
    transitions: list[dict[str, object]] = []
    for s in daily.get("sunrise", []):
        dt = datetime.datetime.fromisoformat(s)
        transitions.append({
            "time": dt,
            "label": f"日出 {dt.strftime('%H:%M')}",
            "day": dt.strftime("%d"),
        })
    for s in daily.get("sunset", []):
        dt = datetime.datetime.fromisoformat(s)
        transitions.append({
            "time": dt,
            "label": f"日落 {dt.strftime('%H:%M')}",
            "day": dt.strftime("%d"),
        })
    transitions.sort(key=lambda x: x["time"])  # type: ignore[arg-type, return-value]

    timer_map = _build_timer_map(t_data)

    # Build hourly rows
    processed_rows: list[dict[str, object]] = []
    for i in range(len(hourly["time"])):
        dt = datetime.datetime.fromisoformat(hourly["time"][i])

        # Skip past hours (2 h buffer)
        if dt.replace(tzinfo=None) < (now - datetime.timedelta(hours=2)).replace(tzinfo=None):
            continue

        # Night-only filter
        if night_only and not (dt.hour >= 18 or dt.hour <= 6):
            continue

        utc_ts = int(dt.astimezone(datetime.UTC).timestamp())
        match = _find_timer_match(timer_map, utc_ts)

        # 7Timer values (fallback to "/")
        s_v: int | str = match["seeing"] if match and match["seeing"] != -9999 else "/"
        tr_v: int | str = match["transparency"] if match and match["transparency"] != -9999 else "/"

        t_v = hourly["temperature_2m"][i]
        d_v = hourly["dew_point_2m"][i]
        w_v = hourly["wind_speed_10m"][i]
        h_v = hourly["relative_humidity_2m"][i]

        tc, tcls = get_m3_color(t_v, "temp", is_night=is_night)
        dc, dcls = _dew_color(d_v, is_night=is_night)
        hc, hcls = get_m3_color(h_v, "humi", is_night=is_night)
        wc, wcls = get_m3_color(w_v, "wind", is_night=is_night)
        sc, scls = get_m3_color(s_v, "seeing", is_night=is_night)
        trc, trcls = get_m3_color(tr_v, "trans", is_night=is_night)

        row: dict[str, object] = {
            "is_transition": False,
            "day": dt.strftime("%d"),
            "hour": dt.strftime("%H"),
            "temp_val": int(t_v),
            "temp_color": tc,
            "temp_cls": tcls,
            "dew_val": int(d_v),
            "dew_color": dc,
            "dew_cls": dcls,
            "humi_val": int(h_v),
            "humi_color": hc,
            "humi_cls": hcls,
            "wind_val": int(w_v),
            "wind_color": wc,
            "wind_cls": wcls,
            "seeing_val": s_v,
            "seeing_color": sc,
            "seeing_cls": scls,
            "trans_val": tr_v,
            "trans_color": trc,
            "trans_cls": trcls,
            "total": hourly["cloud_cover"][i],
            "low": hourly["cloud_cover_low"][i],
            "mid": hourly["cloud_cover_mid"][i],
            "high": hourly["cloud_cover_high"][i],
        }
        processed_rows.append(row)

        # Insert transition rows that fall within this hour
        for trans in transitions:
            t_time: datetime.datetime = trans["time"]  # type: ignore[assignment]
            within_hour = (
                dt.replace(tzinfo=None) <= t_time.replace(tzinfo=None)
                < (dt + datetime.timedelta(hours=1)).replace(tzinfo=None)
            )
            if within_hour:
                processed_rows.append({
                    "is_transition": True,
                    "label": trans["label"],
                    "day": dt.strftime("%d"),
                })

    # Calculate day spans for rowspan
    seen_days: set[str] = set()
    for row in processed_rows:
        if row.get("is_transition"):
            continue
        day: str = row["day"]  # type: ignore[assignment]
        if day not in seen_days:
            row["is_first_of_day"] = True
            row["day_rowspan"] = sum(
                1 for r in processed_rows
                if not r.get("is_transition") and r.get("day") == day
            )
            seen_days.add(day)

    return RenderData(
        lat=round(lat, 4),
        lon=round(lon, 4),
        location_name="",
        ref_time=now.strftime("%Y-%m-%d %H:%M"),
        rows=processed_rows,
        theme_mode=theme_mode,
        model_name="ECMWF+7Timer",
    )
