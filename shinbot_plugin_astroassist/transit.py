"""Satellite pass (过境) prediction via CelesTrak TLE + SGP4.

Fetches TLE orbital elements from CelesTrak and propagates them locally
with the ``sgp4`` package to compute visible passes (rise / peak / set)
over an observer location. Pure-text report, no rendering needed.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

try:
    from sgp4.api import Satrec, jday

    _SGP4_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on host install
    _SGP4_AVAILABLE = False

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

_TLE_URL = "https://celestrak.org/NORAD/elements/gp.php"
_HTTP_TIMEOUT = 15.0

# WGS-84 ellipsoid
_WGS84_A = 6378.137  # km
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = _WGS84_F * (2.0 - _WGS84_F)

_MIN_ELEVATION = 10.0  # default elevation cut-off (degrees)
_STEP_SECONDS = 30.0  # propagation step used to scan for passes
_MAX_PASSES = 10  # per-satellite cap in the report
_MAX_DAYS = 7

_CIRCLED_NUMBERS = ("①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩")

_AZIMUTH_NAMES = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# ------------------------------------------------------------------
# Satellite registry
# ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SatelliteSpec:
    """One satellite that can be asked about."""

    norad_id: int
    name: str
    english: str = ""
    # Standard magnitude at 1000 km range and zero phase angle (approximate;
    # used for apparent-brightness estimation when known).
    mag0: float | None = None


_SATELLITES: tuple[SatelliteSpec, ...] = (
    SatelliteSpec(25544, "国际空间站", "ISS", mag0=-1.3),
    SatelliteSpec(48274, "天宫空间站", "CSS", mag0=0.9),
    SatelliteSpec(20580, "哈勃空间望远镜", "HST", mag0=1.0),
)

_DEFAULT_SATELLITES: tuple[SatelliteSpec, ...] = (
    _SATELLITES[0],  # 国际空间站
    _SATELLITES[1],  # 天宫空间站
)

_NAME_ALIASES: dict[str, int] = {
    "国际空间站": 25544,
    "国际太空站": 25544,
    "iss": 25544,
    "天宫空间站": 48274,
    "天宫": 48274,
    "中国空间站": 48274,
    "天和": 48274,
    "css": 48274,
    "tss": 48274,
    "哈勃空间望远镜": 20580,
    "哈勃望远镜": 20580,
    "哈勃": 20580,
    "hst": 20580,
}

_NORAD_RE = re.compile(r"^\d{1,5}$")


def _spec_for_id(norad_id: int) -> SatelliteSpec:
    for spec in _SATELLITES:
        if spec.norad_id == norad_id:
            return spec
    return SatelliteSpec(norad_id, f"NORAD {norad_id}")


def split_satellite_query(text: str) -> tuple[list[SatelliteSpec] | None, str]:
    """Split *text* into a satellite selection and leftover place text.

    Returns ``(satellites, leftover)``; *satellites* is ``None`` when no
    known satellite name or NORAD id matched, meaning the whole input is
    a place name (default satellite set applies).
    """
    matched_ids: list[int] = []
    leftover: list[str] = []
    tokens = text.split()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "-d":
            i += 2  # skip the flag and its value
            continue
        if token == "-n":
            i += 1
            continue
        low = token.lower()
        if low in _NAME_ALIASES:
            matched_ids.append(_NAME_ALIASES[low])
        elif _NORAD_RE.match(token):
            matched_ids.append(int(token))
        else:
            leftover.append(token)
        i += 1
    if not matched_ids:
        return None, " ".join(leftover)
    specs: list[SatelliteSpec] = []
    seen: set[int] = set()
    for norad_id in matched_ids:
        if norad_id in seen:
            continue
        seen.add(norad_id)
        specs.append(_spec_for_id(norad_id))
    return specs, " ".join(leftover)


# ------------------------------------------------------------------
# TLE fetching (CelesTrak)
# ------------------------------------------------------------------


def _parse_tle(text: str) -> tuple[str, str] | None:
    """Extract the ``(line1, line2)`` pair from a TLE download."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i in range(len(lines) - 1):
        if lines[i].startswith("1 ") and lines[i + 1].startswith("2 "):
            return lines[i], lines[i + 1]
    return None


async def fetch_tle(norad_id: int) -> tuple[str, str] | None:
    """Fetch ``(line1, line2)`` TLE for *norad_id* from CelesTrak."""
    params = {"CATNR": str(norad_id), "FORMAT": "tle"}
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT, follow_redirects=True
    ) as client:
        res = await client.get(_TLE_URL, params=params)
        res.raise_for_status()
    return _parse_tle(res.text)


def build_satrec(line1: str, line2: str) -> Satrec | None:
    """Build an SGP4 propagator from a TLE pair, or ``None`` if invalid."""
    if not (
        line1.startswith("1 ")
        and line2.startswith("2 ")
        and len(line1) >= 60
        and len(line2) >= 60
    ):
        return None
    try:
        return Satrec.twoline2rv(line1, line2)
    except ValueError:
        return None


# ------------------------------------------------------------------
# Coordinate / sun math
# ------------------------------------------------------------------


def _validate_coordinates(lat: float, lon: float) -> None:
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise ValueError(f"无效坐标: ({lat}, {lon})")


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _jday(dt: datetime) -> tuple[float, float]:
    """Return ``(jd, fraction)`` for *dt* as UTC."""
    utc = _as_utc(dt)
    return jday(
        utc.year,
        utc.month,
        utc.day,
        utc.hour,
        utc.minute,
        utc.second + utc.microsecond / 1e6,
    )


def _gmst_rad(jd: float) -> float:
    """Greenwich mean sidereal time in radians (IAU 1982, ~0.1″ accuracy)."""
    t = (jd - 2451545.0) / 36525.0
    gmst_deg = (
        280.46061837
        + 360.98564736629 * (jd - 2451545.0)
        + 0.000387933 * t * t
        - t * t * t / 38710000.0
    )
    return math.radians(gmst_deg % 360.0)


def _geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_m: float) -> tuple[float, float, float]:
    """WGS-84 geodetic → Earth-fixed Cartesian (km)."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    h = alt_m / 1000.0
    n = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
    x = (n + h) * cos_lat * math.cos(lon)
    y = (n + h) * cos_lat * math.sin(lon)
    z = (n * (1.0 - _WGS84_E2) + h) * sin_lat
    return x, y, z


def _topocentric(
    satrec: Satrec,
    jd: float,
    fr: float,
    obs_ecef: tuple[float, float, float],
    lat_deg: float,
    lon_deg: float,
) -> tuple[float, float, float] | None:
    """Return ``(elevation, azimuth, range_km)`` of *satrec* at time ``jd+fr``."""
    err, r, _v = satrec.sgp4(jd, fr)
    if err != 0:
        return None

    # TEME → pseudo-ECEF (rotate by GMST about the z axis)
    theta = _gmst_rad(jd + fr)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    x = r[0] * cos_t + r[1] * sin_t
    y = -r[0] * sin_t + r[1] * cos_t
    z = r[2]

    ux = x - obs_ecef[0]
    uy = y - obs_ecef[1]
    uz = z - obs_ecef[2]
    range_km = math.sqrt(ux * ux + uy * uy + uz * uz)
    if range_km <= 1e-9:
        return None

    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    sin_lon = math.sin(lon)
    cos_lon = math.cos(lon)

    # Local unit vectors: up (geodetic normal), north, east
    up = (cos_lat * cos_lon, cos_lat * sin_lon, sin_lat)
    north = (-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat)
    east = (-sin_lon, cos_lon, 0.0)

    elev = math.asin(
        (ux * up[0] + uy * up[1] + uz * up[2]) / range_km
    )
    east_comp = ux * east[0] + uy * east[1] + uz * east[2]
    north_comp = ux * north[0] + uy * north[1] + uz * north[2]
    az = math.atan2(east_comp, north_comp) % (2.0 * math.pi)

    return math.degrees(elev), math.degrees(az), range_km


def _sun_position(lat: float, lon: float, dt: datetime) -> tuple[float, float]:
    """Solar ``(elevation, azimuth)`` in degrees at *(lat, lon)*.

    Azimuth is measured clockwise from north; elevation is negative below
    the horizon.
    """
    jd, fr = _jday(dt)
    jd_full = jd + fr
    n = jd_full - 2451545.0

    mean_lon = (280.460 + 0.9856474 * n) % 360.0
    mean_anom = math.radians((357.528 + 0.9856003 * n) % 360.0)
    ecl_lon = math.radians(
        mean_lon + 1.915 * math.sin(mean_anom) + 0.020 * math.sin(2.0 * mean_anom)
    )
    obliquity = math.radians(23.439 - 0.0000004 * n)

    ra = math.atan2(
        math.cos(obliquity) * math.sin(ecl_lon), math.cos(ecl_lon)
    )
    dec = math.asin(math.sin(obliquity) * math.sin(ecl_lon))

    gmst_deg = math.degrees(_gmst_rad(jd_full))
    hour_angle = math.radians((gmst_deg + lon - math.degrees(ra)) % 360.0)

    lat_r = math.radians(lat)
    alt = math.asin(
        math.sin(lat_r) * math.sin(dec)
        + math.cos(lat_r) * math.cos(dec) * math.cos(hour_angle)
    )
    # Azimuth measured from south, then converted to from-north.
    az_south = math.atan2(
        math.sin(hour_angle),
        math.cos(hour_angle) * math.sin(lat_r) - math.tan(dec) * math.cos(lat_r),
    )
    az = (math.degrees(az_south) + 180.0) % 360.0
    return math.degrees(alt), az


def sun_elevation(lat: float, lon: float, dt: datetime) -> float:
    """Solar elevation in degrees at *(lat, lon)*; negative means below horizon."""
    return _sun_position(lat, lon, dt)[0]


def visible_magnitude(
    mag0: float,
    range_km: float,
    obs_elev_deg: float,
    obs_az_deg: float,
    sun_elev_deg: float,
    sun_az_deg: float,
) -> float:
    """Estimate a satellite's apparent visual magnitude.

    Model (diffuse-sphere): ``m = m0 + 5*log10(d/1000) - 2.5*log10[sin(α) +
    (π-α)*cos(α)]`` with *m0* the standard magnitude at 1000 km and zero
    phase angle α (the Sun-satellite-observer angle). Roughly matches
    Heavens-Above predictions; expect ±0.5 mag scatter.
    """
    cos_phase = (
        math.sin(math.radians(obs_elev_deg)) * math.sin(math.radians(sun_elev_deg))
        + math.cos(math.radians(obs_elev_deg))
        * math.cos(math.radians(sun_elev_deg))
        * math.cos(math.radians(obs_az_deg - sun_az_deg))
    )
    phase = math.acos(max(-1.0, min(1.0, cos_phase)))
    illuminated = math.sin(phase) + (math.pi - phase) * math.cos(phase)
    illuminated = max(illuminated, 1e-4)
    distance_term = 5.0 * math.log10(max(range_km, 1.0) / 1000.0)
    phase_term = -2.5 * math.log10(illuminated)
    return mag0 + distance_term + phase_term


# ------------------------------------------------------------------
# Pass computation
# ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PassEvent:
    """One visible pass: rise, peak, and set in UTC."""

    start: datetime
    peak: datetime
    end: datetime
    max_elevation: float
    az_start: float
    az_peak: float
    az_end: float
    night: bool
    # Apparent visual magnitude at peak; None when the satellite has no
    # known standard magnitude.
    magnitude: float | None = None

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


def _interp_az(az0: float, az1: float, frac: float) -> float:
    """Linearly interpolate azimuths, unwrapping the 0/360 boundary."""
    delta = az1 - az0
    if delta > 180.0:
        az0 += 360.0
    elif delta < -180.0:
        az1 += 360.0
    value = az0 + (az1 - az0) * frac
    return value % 360.0


def _refine_peak(y0: float, y1: float, y2: float) -> tuple[float, float]:
    """Parabolic refinement around a peak: ``(offset, refined_value)``."""
    denom = y0 - 2.0 * y1 + y2
    if abs(denom) < 1e-12:
        return 0.0, y1
    offset = 0.5 * (y0 - y2) / denom
    offset = max(-1.0, min(1.0, offset))
    refined = y1 - 0.25 * (y0 - y2) * offset
    return offset, refined


def compute_passes(
    satrec: Satrec,
    lat: float,
    lon: float,
    start: datetime,
    end: datetime,
    *,
    min_elevation: float = _MIN_ELEVATION,
    step_seconds: float = _STEP_SECONDS,
    night_only: bool = False,
    altitude_m: float = 0.0,
    mag0: float | None = None,
) -> list[PassEvent]:
    """Compute passes of *satrec* over *(lat, lon)* in ``[start, end]``.

    Passes are found by scanning with a fixed time step and interpolating
    the rise/set instants at *min_elevation*; the peak is refined with a
    parabolic fit. All timestamps are UTC. With *mag0* set, each pass also
    carries an estimated apparent magnitude at its peak.
    """
    _validate_coordinates(lat, lon)
    start = _as_utc(start)
    end = _as_utc(end)
    if end <= start or step_seconds <= 0:
        return []

    step = timedelta(seconds=float(step_seconds))
    obs_ecef = _geodetic_to_ecef(lat, lon, altitude_m)

    events: list[PassEvent] = []
    in_run = False
    run_start_t: datetime | None = None
    run_start_az = 0.0
    samples: list[tuple[datetime, float, float, float]] = []
    prev: tuple[datetime, float, float, float] | None = None
    t = start

    def finish_run(below_t: datetime | None, below_elev: float, below_az: float) -> None:
        nonlocal in_run, samples
        if not samples:
            return
        # Peak via parabolic fit around the highest sample
        peak_i = max(range(len(samples)), key=lambda i: samples[i][1])
        t_pk, e_pk, az_pk, r_pk = samples[peak_i]
        if 0 < peak_i < len(samples) - 1:
            offset, refined = _refine_peak(
                samples[peak_i - 1][1], e_pk, samples[peak_i + 1][1]
            )
            t_pk = t_pk + offset * step
            e_pk = refined

        if below_t is None:
            # Window ends mid-pass: set time at the last sample
            t_end, az_end = samples[-1][0], samples[-1][2]
        else:
            t_last, e_last, az_last, _r_last = samples[-1]
            denom = below_elev - e_last
            frac = (min_elevation - e_last) / denom if abs(denom) > 1e-9 else 0.0
            frac = max(0.0, min(1.0, frac))
            t_end = t_last + frac * step
            az_end = _interp_az(az_last, below_az, frac)

        night = sun_elevation(lat, lon, t_pk) < 0.0
        if night_only and not night:
            return
        magnitude: float | None = None
        if mag0 is not None:
            sun_elev, sun_az = _sun_position(lat, lon, t_pk)
            magnitude = visible_magnitude(mag0, r_pk, e_pk, az_pk, sun_elev, sun_az)
        events.append(
            PassEvent(
                start=run_start_t or samples[0][0],
                peak=t_pk,
                end=t_end,
                max_elevation=e_pk,
                az_start=run_start_az,
                az_peak=az_pk,
                az_end=az_end,
                night=night,
                magnitude=magnitude,
            )
        )

    while t <= end:
        jd, fr = _jday(t)
        topo = _topocentric(satrec, jd, fr, obs_ecef, lat, lon)
        if topo is None:
            if in_run:
                finish_run(None, 0.0, 0.0)
                in_run = False
                samples = []
            prev = None
            t += step
            continue
        elev, az, range_km = topo
        cur = (t, elev, az, range_km)

        if elev >= min_elevation:
            if not in_run:
                in_run = True
                samples = []
                if prev is not None:
                    t_prev, e_prev, az_prev, _r_prev = prev
                    denom = elev - e_prev
                    frac = (
                        (min_elevation - e_prev) / denom if abs(denom) > 1e-9 else 0.0
                    )
                    frac = max(0.0, min(1.0, frac))
                    run_start_t = t_prev + frac * step
                    run_start_az = _interp_az(az_prev, az, frac)
                else:
                    run_start_t = t
                    run_start_az = az
            samples.append(cur)
        else:
            if in_run:
                finish_run(t, elev, az)
                in_run = False
                samples = []
        prev = cur
        t += step

    if in_run:
        finish_run(None, 0.0, 0.0)
    return events


# ------------------------------------------------------------------
# Report formatting
# ------------------------------------------------------------------


def _azimuth_name(deg: float) -> str:
    idx = int(((deg % 360.0) + 22.5) // 45.0) % 8
    return _AZIMUTH_NAMES[idx]


def _format_coord(value: float, positive: str, negative: str) -> str:
    if value < 0:
        return f"{-value:.4f}°{negative}"
    return f"{value:.4f}°{positive}"


def _format_pass_line(pass_event: PassEvent, tz: timezone) -> str:
    start_l = pass_event.start.astimezone(tz)
    end_l = pass_event.end.astimezone(tz)
    peak_l = pass_event.peak.astimezone(tz)
    duration_min = max(1, round(pass_event.duration.total_seconds() / 60.0))
    day_flag = "🌙 夜间" if pass_event.night else "☀️ 白天"
    brightness = ""
    if pass_event.magnitude is not None:
        brightness = f" · 亮度 {pass_event.magnitude:+.1f} 等"
    return (
        f"{peak_l.month:02d}-{peak_l.day:02d} ({_WEEKDAYS[peak_l.weekday()]}) "
        f"{start_l:%H:%M}–{end_l:%H:%M} · 最高 {round(pass_event.max_elevation)}°"
        f" ({_azimuth_name(pass_event.az_peak)}) · "
        f"{_azimuth_name(pass_event.az_start)}→{_azimuth_name(pass_event.az_end)} · "
        f"{duration_min} 分钟{brightness} · {day_flag}"
    )


def _format_satellite_block(
    spec: SatelliteSpec,
    passes: list[PassEvent],
    tz: timezone,
) -> str:
    title = f"【{spec.name} {spec.english}】" if spec.english else f"【{spec.name}】"
    if not passes:
        return f"{title}\n（无符合条件的过境）"
    lines = [title]
    for i, pass_event in enumerate(passes[:_MAX_PASSES], start=1):
        lines.append(f"{_CIRCLED_NUMBERS[i - 1]} {_format_pass_line(pass_event, tz)}")
    if len(passes) > _MAX_PASSES:
        lines.append(f"… 共 {len(passes)} 次，仅显示前 {_MAX_PASSES} 次")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class TransitReport:
    """Structured transit report, split into foldable message sections."""

    header: str
    blocks: tuple[str, ...]
    warnings: tuple[str, ...]
    footer: str

    @property
    def sections(self) -> list[str]:
        """Sections suitable for a merged-forward (合并转发) message."""
        parts: list[str] = [self.header]
        parts.extend(self.blocks)
        if self.warnings:
            parts.append("\n".join(self.warnings))
        parts.append(self.footer)
        return parts

    def to_text(self) -> str:
        """The plain-text rendering of the report."""
        return "\n".join(self.sections)


def build_transit_report(
    location_name: str,
    lat: float,
    lon: float,
    start: datetime,
    end: datetime,
    blocks: list[str],
    warnings: list[str],
    *,
    night_only: bool,
    min_elevation: float,
) -> TransitReport:
    """Assemble a structured transit report (local-time display)."""
    tz = _local_tz()
    start_l = _as_utc(start).astimezone(tz)
    end_l = _as_utc(end).astimezone(tz)
    days = max(1, round((_as_utc(end) - _as_utc(start)).total_seconds() / 86400.0))

    header = "\n".join(
        [
            f"🛰️ 过境卫星预报 | {location_name or '当前位置'}",
            f"📍 {_format_coord(lat, 'N', 'S')}, {_format_coord(lon, 'E', 'W')}",
            f"📅 未来 {days} 天：{start_l:%m-%d %H:%M} → {end_l:%m-%d %H:%M}",
            "━━━━━━━━━━━━━━━",
        ]
    )
    notes = [f"高度角阈值 ≥ {min_elevation:g}°"]
    if night_only:
        notes.append("🌙 仅显示夜间（太阳在地平线下）过境")
    footer = "\n".join(
        [
            "━━━━━━━━━━━━━━━",
            " · ".join(notes),
            "数据源：CelesTrak TLE + SGP4 本地推算",
        ]
    )
    return TransitReport(
        header=header,
        blocks=tuple(blocks),
        warnings=tuple(warnings),
        footer=footer,
    )


# ------------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def fetch_transit_report(
    lat: float,
    lon: float,
    location_name: str,
    *,
    query: str = "",
    days: int = 3,
    night_only: bool = False,
    min_elevation: float = _MIN_ELEVATION,
) -> TransitReport:
    """Fetch TLEs and compute a satellite pass report for *(lat, lon)*."""
    if not _SGP4_AVAILABLE:
        raise RuntimeError("缺少 sgp4 依赖，无法进行轨道推算。请安装 sgp4。")
    _validate_coordinates(lat, lon)
    days = max(1, min(_MAX_DAYS, int(days)))
    min_elevation = max(1.0, min(30.0, float(min_elevation)))

    satellites, _leftover = split_satellite_query(query)
    specs = satellites if satellites is not None else list(_DEFAULT_SATELLITES)

    start = _utcnow()
    end = start + timedelta(days=days)

    tles = await asyncio.gather(
        *(fetch_tle(spec.norad_id) for spec in specs),
        return_exceptions=True,
    )

    blocks: list[str] = []
    warnings: list[str] = []
    for spec, tle in zip(specs, tles):
        if isinstance(tle, Exception):
            warnings.append(f"⚠️ {spec.name}: TLE 获取失败 ({tle})")
            continue
        if tle is None:
            warnings.append(f"⚠️ {spec.name}: 未找到 TLE 数据 (NORAD {spec.norad_id})")
            continue
        satrec = build_satrec(tle[0], tle[1])
        if satrec is None:
            warnings.append(f"⚠️ {spec.name}: TLE 解析失败")
            continue
        passes = compute_passes(
            satrec,
            lat,
            lon,
            start,
            end,
            min_elevation=min_elevation,
            night_only=night_only,
            mag0=spec.mag0,
        )
        blocks.append(_format_satellite_block(spec, passes, _local_tz()))

    if not blocks:
        raise ValueError("所有卫星均无法获取轨道数据")

    return build_transit_report(
        location_name,
        lat,
        lon,
        start,
        end,
        blocks,
        warnings,
        night_only=night_only,
        min_elevation=min_elevation,
    )


def _local_tz() -> timezone:
    return datetime.now().astimezone().tzinfo
