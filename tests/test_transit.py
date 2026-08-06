"""Tests for satellite transit (过境) prediction and command wiring."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shinbot_plugin_astroassist.commands import register_commands
from shinbot_plugin_astroassist.models import LocationData
from shinbot_plugin_astroassist.storage import LocationStore
from shinbot_plugin_astroassist.transit import (
    TransitReport,
    _azimuth_name,
    _parse_tle,
    build_satrec,
    build_transit_report,
    compute_passes,
    fetch_transit_report,
    split_satellite_query,
    sun_elevation,
    visible_magnitude,
)

# Fixed ISS TLE (epoch 2026-08-06) — deterministic propagation source.
_ISS_LINE1 = "1 25544U 98067A   26218.05391056  .00003997  00000+0  79690-4 0  9990"
_ISS_LINE2 = "2 25544  51.6321  53.3065 0007216  17.1615 342.9616 15.49359774579487"

# Fixed CSS (天和) TLE, same epoch — used for the 天文通 comparison pass.
_CSS_LINE1 = "1 48274U 21035A   26218.13147276  .00013245  00000+0  17031-3 0  9995"
_CSS_LINE2 = "2 48274  41.4698  18.8075 0001032 269.9767  90.0953 15.58844096300964"

_BEIJING = (39.9042, 116.4074)

_WINDOW_START = datetime(2026, 8, 6, 0, 0, 0, tzinfo=timezone.utc)
_WINDOW_END = datetime(2026, 8, 8, 0, 0, 0, tzinfo=timezone.utc)


class _FakePlugin:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.commands: dict[str, dict[str, Any]] = {}

    def on_command(self, name: str, **kwargs: Any) -> Any:
        def decorator(func: Any) -> Any:
            self.commands[name] = {**kwargs, "handler": func}
            return func

        return decorator


class _Ctx:
    def __init__(self) -> None:
        self.session_id = "session-1"
        self.sent: list[Any] = []
        self.stopped = False

    async def send(self, content: Any) -> None:
        self.sent.append(content)

    def stop(self) -> None:
        self.stopped = True


def _register(tmp_path: Path) -> _FakePlugin:
    plugin = _FakePlugin(tmp_path)
    register_commands(
        plugin,
        SimpleNamespace(amap_key=""),
        LocationStore(tmp_path),
        tmp_path / "template.html",
    )
    return plugin


# ------------------------------------------------------------------
# Query splitting
# ------------------------------------------------------------------


def test_split_satellite_query_matches_names_and_norad_ids() -> None:
    specs, leftover = split_satellite_query("天宫 北京")
    assert [s.norad_id for s in specs] == [48274]
    assert leftover == "北京"

    specs, leftover = split_satellite_query("ISS")
    assert [s.norad_id for s in specs] == [25544]
    assert leftover == ""

    specs, leftover = split_satellite_query("25544 上海")
    assert [s.norad_id for s in specs] == [25544]
    assert leftover == "上海"

    specs, leftover = split_satellite_query("哈勃 天宫")
    assert [s.norad_id for s in specs] == [20580, 48274]
    assert leftover == ""


def test_split_satellite_query_unknown_text_is_place() -> None:
    specs, leftover = split_satellite_query("北京")
    assert specs is None
    assert leftover == "北京"

    specs, leftover = split_satellite_query("")
    assert specs is None
    assert leftover == ""


def test_split_satellite_query_ignores_flags() -> None:
    specs, leftover = split_satellite_query("天宫 -d 2 -n")
    assert [s.norad_id for s in specs] == [48274]
    assert leftover == ""


# ------------------------------------------------------------------
# TLE handling
# ------------------------------------------------------------------


def test_parse_tle_extracts_line_pair() -> None:
    text = (
        "ISS (ZARYA)             \n"
        f"{_ISS_LINE1}\n"
        f"{_ISS_LINE2}\n"
    )
    assert _parse_tle(text) == (_ISS_LINE1, _ISS_LINE2)


def test_parse_tle_rejects_non_tle_body() -> None:
    assert _parse_tle("Invalid query: ...\n") is None


def test_build_satrec_rejects_garbage() -> None:
    assert build_satrec("not a tle", "still not a tle") is None


def test_build_satrec_accepts_valid_tle() -> None:
    assert build_satrec(_ISS_LINE1, _ISS_LINE2) is not None


# ------------------------------------------------------------------
# Sun elevation
# ------------------------------------------------------------------


def test_sun_elevation_noon_higher_than_midnight_beijing() -> None:
    lat, lon = _BEIJING
    noon_bjt = _WINDOW_START.replace(hour=4)  # 12:00 Beijing
    midnight_bjt = _WINDOW_START.replace(hour=16)  # 00:00 Beijing
    noon_elev = sun_elevation(lat, lon, noon_bjt)
    midnight_elev = sun_elevation(lat, lon, midnight_bjt)
    assert noon_elev > 40.0
    assert midnight_elev < 0.0
    assert noon_elev > midnight_elev


# ------------------------------------------------------------------
# Pass computation (deterministic against the embedded ISS TLE)
# ------------------------------------------------------------------


def test_compute_passes_finds_deterministic_passes() -> None:
    satrec = build_satrec(_ISS_LINE1, _ISS_LINE2)
    assert satrec is not None
    lat, lon = _BEIJING
    passes = compute_passes(
        satrec, lat, lon, _WINDOW_START, _WINDOW_END, min_elevation=10.0
    )

    # Golden values: exact pass peaks for this TLE / window / observer.
    assert [(p.peak.replace(microsecond=0), round(p.max_elevation, 1)) for p in passes] == [
        (datetime(2026, 8, 6, 1, 31, 34, tzinfo=timezone.utc), 82.5),
        (datetime(2026, 8, 6, 3, 8, 46, tzinfo=timezone.utc), 18.5),
        (datetime(2026, 8, 6, 4, 46, 36, tzinfo=timezone.utc), 11.2),
        (datetime(2026, 8, 6, 6, 24, 26, tzinfo=timezone.utc), 19.0),
        (datetime(2026, 8, 6, 8, 1, 32, tzinfo=timezone.utc), 81.9),
        (datetime(2026, 8, 7, 0, 43, 53, tzinfo=timezone.utc), 47.0),
        (datetime(2026, 8, 7, 2, 20, 48, tzinfo=timezone.utc), 24.4),
        (datetime(2026, 8, 7, 3, 58, 31, tzinfo=timezone.utc), 11.6),
        (datetime(2026, 8, 7, 5, 36, 25, tzinfo=timezone.utc), 15.2),
        (datetime(2026, 8, 7, 7, 13, 47, tzinfo=timezone.utc), 56.4),
        (datetime(2026, 8, 7, 8, 50, 14, tzinfo=timezone.utc), 15.4),
        (datetime(2026, 8, 7, 23, 56, 11, tzinfo=timezone.utc), 26.4),
    ]


def test_compute_passes_invariants() -> None:
    satrec = build_satrec(_ISS_LINE1, _ISS_LINE2)
    assert satrec is not None
    lat, lon = _BEIJING
    passes = compute_passes(
        satrec, lat, lon, _WINDOW_START, _WINDOW_END, min_elevation=10.0
    )
    assert passes
    for p in passes:
        assert p.start < p.peak < p.end
        assert p.max_elevation >= 10.0
        duration = p.duration.total_seconds() / 60.0
        assert 1.0 <= duration <= 20.0
        for az in (p.az_start, p.az_peak, p.az_end):
            assert 0.0 <= az < 360.0


def test_compute_passes_night_only_and_min_elevation_filter() -> None:
    satrec = build_satrec(_ISS_LINE1, _ISS_LINE2)
    assert satrec is not None
    lat, lon = _BEIJING

    all_passes = compute_passes(
        satrec, lat, lon, _WINDOW_START, _WINDOW_END, min_elevation=10.0
    )
    # Beijing: every pass in this window happens in daylight, so the
    # night-only filter must return nothing.
    night_passes = compute_passes(
        satrec,
        lat,
        lon,
        _WINDOW_START,
        _WINDOW_END,
        min_elevation=10.0,
        night_only=True,
    )
    assert night_passes == []
    assert all(p.night for p in night_passes)
    assert len(night_passes) < len(all_passes)

    # Singapore: mixed day/night visibility — night_only keeps exactly the
    # nighttime subset.
    all_sg = compute_passes(
        satrec, 1.35, 103.8, _WINDOW_START, _WINDOW_END, min_elevation=10.0
    )
    night_sg = compute_passes(
        satrec,
        1.35,
        103.8,
        _WINDOW_START,
        _WINDOW_END,
        min_elevation=10.0,
        night_only=True,
    )
    assert night_sg
    assert all(p.night for p in night_sg)
    assert set(night_sg) < set(all_sg)

    high_passes = compute_passes(
        satrec, lat, lon, _WINDOW_START, _WINDOW_END, min_elevation=30.0
    )
    assert high_passes
    assert all(p.max_elevation >= 30.0 for p in high_passes)
    assert len(high_passes) < len(all_passes)


def test_compute_passes_rejects_invalid_coordinates() -> None:
    satrec = build_satrec(_ISS_LINE1, _ISS_LINE2)
    assert satrec is not None
    with pytest.raises(ValueError):
        compute_passes(satrec, 91.0, 0.0, _WINDOW_START, _WINDOW_END)


def test_compute_passes_magnitude_present_with_mag0() -> None:
    satrec = build_satrec(_ISS_LINE1, _ISS_LINE2)
    assert satrec is not None
    passes = compute_passes(
        satrec,
        *_BEIJING,
        _WINDOW_START,
        _WINDOW_END,
        min_elevation=10.0,
        mag0=-1.3,
    )
    assert passes
    assert all(p.magnitude is not None for p in passes)
    # Plausible ISS range: bright overhead pass to faint grazing pass.
    assert all(-5.5 <= p.magnitude <= 4.0 for p in passes if p.magnitude is not None)

    # Without mag0 no magnitude is attached.
    passes_plain = compute_passes(
        satrec, *_BEIJING, _WINDOW_START, _WINDOW_END, min_elevation=10.0
    )
    assert all(p.magnitude is None for p in passes_plain)


def test_compute_passes_reports_peak_brightness_matching_reference() -> None:
    # 天文通 shows this exact pass (咸宁, CSS, 08-09 05:24 BJT) at -0.7 mag.
    # The reported value is the pass's peak brightness, which occurs at
    # ~05:28 (near max elevation 38°), not at the 10° rise time.
    satrec = build_satrec(_CSS_LINE1, _CSS_LINE2)
    assert satrec is not None
    start = datetime(2026, 8, 8, 21, 15, 0, tzinfo=timezone.utc)  # 08-09 05:15 BJT
    end = start + timedelta(minutes=25)
    passes = compute_passes(
        satrec, 29.8493, 114.4708, start, end, min_elevation=10.0, mag0=0.9
    )
    assert len(passes) == 1
    assert round(passes[0].max_elevation) == 38
    assert round(passes[0].magnitude, 1) == -0.7


# ------------------------------------------------------------------
# Brightness model
# ------------------------------------------------------------------


def test_visible_magnitude_matches_reference_value() -> None:
    # Heavens-Above style reference: ISS (m0=-1.3) at 483 km, 113° phase
    # angle → about -2.0 mag. Geometry chosen so the observer-sun line
    # subtends 113° from the observer-satellite line.
    import math as _math

    cos_phase = _math.cos(_math.radians(113.0))
    sun_az = _math.degrees(_math.acos(2.0 * (cos_phase + 0.5)))
    mag = visible_magnitude(-1.3, 483.0, 45.0, 0.0, -45.0, sun_az)
    assert mag == pytest.approx(-2.0, abs=0.1)


def test_visible_magnitude_distance_and_phase_trends() -> None:
    overhead_full = visible_magnitude(-1.3, 400.0, 60.0, 0.0, 60.0, 0.0)
    horizon = visible_magnitude(-1.3, 2000.0, 20.0, 0.0, 20.0, 0.0)
    backlit = visible_magnitude(-1.3, 400.0, 60.0, 0.0, -60.0, 180.0)

    assert overhead_full < horizon  # closer is brighter
    assert overhead_full < backlit  # full phase is brighter than backlit


# ------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------


def test_azimuth_names_cover_compass_octants() -> None:
    assert _azimuth_name(0.0) == "北"
    assert _azimuth_name(45.0) == "东北"
    assert _azimuth_name(90.0) == "东"
    assert _azimuth_name(180.0) == "南"
    assert _azimuth_name(270.0) == "西"
    assert _azimuth_name(359.0) == "北"
    assert _azimuth_name(22.4) == "北"
    assert _azimuth_name(22.6) == "东北"


def test_build_transit_report_assembles_sections() -> None:
    start = _WINDOW_START
    end = _WINDOW_START + timedelta(days=3)
    report = build_transit_report(
        "北京",
        *_BEIJING,
        start,
        end,
        ["【国际空间站 ISS】\n① 08-06 (周四) 21:00–21:10 · 最高 45° (西南) · 西南→东北 · 9 分钟 · 🌙 夜间"],
        ["⚠️ 哈勃空间望远镜: TLE 获取失败"],
        night_only=False,
        min_elevation=10.0,
    )
    assert report.header.startswith("🛰️ 过境卫星预报 | 北京")
    assert "39.9042°N" in report.header and "116.4074°E" in report.header
    assert "未来 3 天" in report.header
    assert report.blocks == (
        "【国际空间站 ISS】\n① 08-06 (周四) 21:00–21:10 · 最高 45° (西南) · 西南→东北 · 9 分钟 · 🌙 夜间",
    )
    assert report.warnings == ("⚠️ 哈勃空间望远镜: TLE 获取失败",)
    assert "高度角阈值 ≥ 10°" in report.footer
    assert "CelesTrak TLE + SGP4" in report.footer

    text = report.to_text()
    assert "国际空间站" in text
    assert "哈勃空间望远镜" in text
    assert report.sections == [report.header, *report.blocks, *report.warnings, report.footer]


# ------------------------------------------------------------------
# fetch_transit_report orchestration
# ------------------------------------------------------------------


def test_fetch_transit_report_builds_blocks_and_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shinbot_plugin_astroassist.transit as transit

    fixed_now = _WINDOW_START
    monkeypatch.setattr(transit, "_utcnow", lambda: fixed_now)

    async def fake_fetch_tle(norad_id: int) -> tuple[str, str] | None:
        if norad_id == 25544:
            return _ISS_LINE1, _ISS_LINE2
        return None

    monkeypatch.setattr(transit, "fetch_tle", fake_fetch_tle)

    report = None
    import asyncio

    async def run() -> None:
        nonlocal report
        report = await fetch_transit_report(
            *_BEIJING, "北京", query="国际空间站", days=2, night_only=False
        )

    asyncio.run(run())

    assert report is not None
    assert isinstance(report, TransitReport)
    assert any("国际空间站" in block for block in report.blocks)
    assert "亮度" in report.blocks[0]  # ISS has a known standard magnitude
    assert "过境卫星预报" in report.to_text()


def test_fetch_transit_report_defaults_to_night_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shinbot_plugin_astroassist.transit as transit

    monkeypatch.setattr(transit, "_utcnow", lambda: _WINDOW_START)

    async def fake_fetch_tle(norad_id: int) -> tuple[str, str]:
        return _ISS_LINE1, _ISS_LINE2

    monkeypatch.setattr(transit, "fetch_tle", fake_fetch_tle)

    import asyncio

    # All ISS passes over Beijing in this window occur in daylight, so the
    # night-only default filters every one of them out.
    report = asyncio.run(
        fetch_transit_report(*_BEIJING, "北京", query="国际空间站", days=2)
    )
    assert "（无符合条件的过境）" in report.blocks[0]


def test_fetch_transit_report_reports_missing_tle_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shinbot_plugin_astroassist.transit as transit

    monkeypatch.setattr(transit, "_utcnow", lambda: _WINDOW_START)

    async def fake_fetch_tle(norad_id: int) -> tuple[str, str] | None:
        if norad_id == 25544:
            return _ISS_LINE1, _ISS_LINE2
        return None

    monkeypatch.setattr(transit, "fetch_tle", fake_fetch_tle)

    import asyncio

    report = asyncio.run(
        fetch_transit_report(
            *_BEIJING, "北京", query="国际空间站 天宫", days=1, night_only=False
        )
    )
    assert any("国际空间站" in block for block in report.blocks)
    assert "⚠️ 天宫空间站" in report.to_text()


# ------------------------------------------------------------------
# Command wiring
# ------------------------------------------------------------------


def test_register_commands_declares_transit_command(tmp_path: Path) -> None:
    plugin = _register(tmp_path)

    assert "过境卫星" in plugin.commands
    assert plugin.commands["过境卫星"]["aliases"] == ["卫星过境", "transit", "satpass"]
    assert "过境" in plugin.commands["过境卫星"]["description"]
    assert plugin.commands["过境卫星"]["usage"].startswith("!过境卫星")


@pytest.mark.asyncio
async def test_transit_command_uses_stored_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shinbot_plugin_astroassist.commands as commands

    captured: dict[str, Any] = {}

    async def fake_fetch(
        lat: float, lon: float, location_name: str, **kwargs: Any
    ) -> TransitReport:
        captured["lat"] = lat
        captured["lon"] = lon
        captured["name"] = location_name
        captured["kwargs"] = kwargs
        return TransitReport(
            header=f"🛰️ 过境卫星预报 | {location_name}",
            blocks=(),
            warnings=(),
            footer="",
        )

    monkeypatch.setattr(commands, "fetch_transit_report", fake_fetch)

    store = LocationStore(tmp_path)
    await store.put(
        "session-1", LocationData(lat=31.2304, lon=121.4737, name="上海")
    )

    plugin = _FakePlugin(tmp_path)
    register_commands(
        plugin,
        SimpleNamespace(amap_key=""),
        store,
        tmp_path / "template.html",
    )

    ctx = _Ctx()
    await plugin.commands["过境卫星"]["handler"](ctx, "天宫 -d 2")

    assert ctx.stopped is True
    assert captured["lat"] == 31.2304
    assert captured["lon"] == 121.4737
    assert captured["name"] == "上海"
    # Night-only is the default; the handler passes no night_only override.
    assert captured["kwargs"] == {
        "query": "天宫",
        "days": 2,
    }
    assert "上海" in ctx.sent[0]


@pytest.mark.asyncio
async def test_transit_command_help_does_not_resolve_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shinbot_plugin_astroassist.commands as commands

    async def fake_fetch(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("fetch must not run for help")

    monkeypatch.setattr(commands, "fetch_transit_report", fake_fetch)

    plugin = _register(tmp_path)
    ctx = _Ctx()
    await plugin.commands["过境卫星"]["handler"](ctx, "help")

    assert ctx.stopped is True
    assert "过境卫星预报 | 说明" in ctx.sent[0]


@pytest.mark.asyncio
async def test_transit_command_sends_folded_forward_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shinbot_plugin_astroassist.commands as commands

    async def fake_fetch(
        lat: float, lon: float, location_name: str, **kwargs: Any
    ) -> TransitReport:
        return TransitReport(
            header=f"🛰️ 过境卫星预报 | {location_name}",
            blocks=(
                "【国际空间站 ISS】\n① 08-07 (周五) 08:40–08:47 · 最高 47° (东南) · 西南→东北 · 6 分钟 · ☀️ 白天",
                "【天宫空间站 CSS】\n① 08-07 (周五) 07:21–07:26 · 最高 30° (东南) · 西南→东 · 6 分钟 · ☀️ 白天",
            ),
            warnings=("⚠️ 哈勃空间望远镜: TLE 获取失败",),
            footer="━━━━━━━━━━━━━━━\n高度角阈值 ≥ 10°\n数据源：CelesTrak TLE + SGP4 本地推算",
        )

    monkeypatch.setattr(commands, "fetch_transit_report", fake_fetch)

    store = LocationStore(tmp_path)
    await store.put(
        "session-1", LocationData(lat=31.2304, lon=121.4737, name="上海")
    )

    plugin = _FakePlugin(tmp_path)
    register_commands(
        plugin,
        SimpleNamespace(amap_key=""),
        store,
        tmp_path / "template.html",
    )

    ctx = _Ctx()
    ctx.adapter = SimpleNamespace(platform="onebot_v11")
    await plugin.commands["过境卫星"]["handler"](ctx, "")

    assert ctx.stopped is True
    forward = ctx.sent[0][0]
    assert forward["type"] == "message"
    assert forward["attrs"]["forward"] == "true"
    nodes = forward["children"]
    assert len(nodes) == 5  # header, 2 satellite blocks, warnings, footer
    assert all(node["attrs"]["nickname"] == "AstroAssist" for node in nodes)
    contents = [node["children"][0]["attrs"]["content"] for node in nodes]
    assert contents[0].startswith("🛰️ 过境卫星预报")
    assert "国际空间站" in contents[1]
    assert "天宫空间站" in contents[2]
    assert "哈勃空间望远镜" in contents[3]
    assert "数据源" in contents[4]
