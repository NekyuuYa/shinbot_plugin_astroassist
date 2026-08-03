"""Tests for NMC radar matching and nearby-station fallback."""

from __future__ import annotations

import pytest

from shinbot_plugin_astroassist.geo import GeocodeResult
from shinbot_plugin_astroassist.radar import (
    _haversine_km,
    _parse_station_links,
    fetch_radar,
    find_nearby_radar_stations,
    resolve_radar_page,
)


_RADAR_HTML = """
<div class="col-xs-12 time actived"
  data-img="https://image.nmc.cn/product/2026/08/04/RDCP/WSA/latest.PNG?v=1"
  data-time="08/04 12:00"></div>
"""

# This is the shape of the "城市/地区" navigation on an NMC province page.
# The links and paths are real NMC station entries; coordinates are supplied
# by the mocked AMap calls below, as they are at runtime in the plugin.
_JIANGSU_NAV_HTML = """
<div class="p-nav nav3">
  <a href="/publish/radar/jiang-su/nan-jing.htm">南京</a>
  <a href="/publish/radar/jiang-su/nan-tong.htm">南通</a>
  <a href="/publish/radar/jiang-su/chang-zhou.htm">常州</a>
  <a href="/publish/radar/jiang-su/yan-cheng.htm">盐城</a>
</div></div><div class="row">
"""


def _reset_radar_caches() -> None:
    import shinbot_plugin_astroassist.radar as radar

    radar._STATION_TABLE_CACHE.clear()
    radar._STATION_COORD_CACHE.clear()
    radar._TARGET_COORD_CACHE.clear()


def test_resolve_radar_page_keeps_exact_station_matching() -> None:
    assert resolve_radar_page("武汉") == (
        "/publish/radar/hu-bei/wu-han.htm",
        "武汉",
    )


def test_haversine_distance_uses_great_circle_geometry() -> None:
    # One degree of longitude at the equator is approximately 111 km.
    assert 111 < _haversine_km(0, 0, 0, 1) < 112


def test_parse_station_links_reads_nmc_navigation() -> None:
    stations = _parse_station_links(_JIANGSU_NAV_HTML, "江苏")

    assert [(station.label, station.path) for station in stations] == [
        ("南京", "/publish/radar/jiang-su/nan-jing.htm"),
        ("南通", "/publish/radar/jiang-su/nan-tong.htm"),
        ("常州", "/publish/radar/jiang-su/chang-zhou.htm"),
        ("盐城", "/publish/radar/jiang-su/yan-cheng.htm"),
    ]


async def _fake_target_geocode(address: str, api_key: str) -> GeocodeResult:
    assert api_key == "test-amap-key"
    assert address in {"无锡", "江苏无锡"}
    return GeocodeResult(
        latitude=0.0,
        longitude=0.0,
        province="江苏省",
        city="无锡市",
        district="",
    )


async def _fake_station_geocode(address: str, api_key: str) -> tuple[float, float]:
    assert api_key == "test-amap-key"
    # Relative fixture coordinates keep this test independent of any
    # handwritten production coordinate table.
    coordinates = {
        "江苏常州": (0.1, 0.1),
        "江苏南京": (1.0, 1.0),
        "江苏南通": (0.8, 0.2),
        "江苏盐城": (2.0, 0.0),
    }
    return coordinates[address]


@pytest.mark.asyncio
async def test_wuxi_gets_real_jiangsu_stations_ranked_by_distance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shinbot_plugin_astroassist.radar as radar

    _reset_radar_caches()

    async def fake_fetch_page(path: str) -> str:
        assert path == "/publish/radar/jiang-su/nan-jing.htm"
        return _JIANGSU_NAV_HTML

    monkeypatch.setattr(radar, "_fetch_page", fake_fetch_page)
    monkeypatch.setattr(radar, "amap_geocode_detail", _fake_target_geocode)
    monkeypatch.setattr(radar, "amap_geocode", _fake_station_geocode)

    candidates = await find_nearby_radar_stations("无锡", "test-amap-key")

    assert [candidate.station.label for candidate in candidates] == [
        "常州",
        "南通",
        "南京",
    ]
    assert candidates[0].distance_km < candidates[1].distance_km


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["无锡", "江苏无锡"])
async def test_fetch_radar_uses_nearest_nmc_station_for_wuxi(
    query: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shinbot_plugin_astroassist.radar as radar

    _reset_radar_caches()

    async def fake_fetch_page(path: str) -> str:
        if path == "/publish/radar/jiang-su/nan-jing.htm":
            return _JIANGSU_NAV_HTML
        assert path == "/publish/radar/jiang-su/chang-zhou.htm"
        return _RADAR_HTML

    monkeypatch.setattr(radar, "_fetch_page", fake_fetch_page)
    monkeypatch.setattr(radar, "amap_geocode_detail", _fake_target_geocode)
    monkeypatch.setattr(radar, "amap_geocode", _fake_station_geocode)

    url, obs_time, label = await fetch_radar(query, amap_key="test-amap-key")

    assert url.endswith("latest.PNG?v=1")
    assert obs_time == "08/04 12:00"
    assert label.startswith("常州（")
    assert "无锡附近" in label
