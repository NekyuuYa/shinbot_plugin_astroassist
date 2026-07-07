"""AMap geocoding with GCJ-02 → WGS-84 coordinate conversion."""

from __future__ import annotations

import math

import httpx

# ------------------------------------------------------------------
# GCJ-02 → WGS-84 conversion
# ------------------------------------------------------------------

_PI = math.pi
_A = 6378245.0  # semi-major axis
_EE = 0.00669342162296594  # eccentricity squared


def _out_of_china(lng: float, lat: float) -> bool:
    """Rough bounding-box check for the GCJ-02 offset zone."""
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(lng: float, lat: float) -> float:
    ret = (
        -100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat
        + 0.1 * lng * lat + 0.2 * math.sqrt(abs(lng))
    )
    ret += (
        (20.0 * math.sin(6.0 * lng * _PI) + 20.0 * math.sin(2.0 * lng * _PI))
        * 2.0 / 3.0
    )
    ret += (
        (20.0 * math.sin(lat * _PI) + 40.0 * math.sin(lat / 3.0 * _PI))
        * 2.0 / 3.0
    )
    ret += (
        (160.0 * math.sin(lat / 12.0 * _PI) + 320.0 * math.sin(lat * _PI / 30.0))
        * 2.0 / 3.0
    )
    return ret


def _transform_lng(lng: float, lat: float) -> float:
    ret = (
        300.0 + lng + 2.0 * lat + 0.1 * lng * lng
        + 0.1 * lng * lat + 0.1 * math.sqrt(abs(lng))
    )
    ret += (
        (20.0 * math.sin(6.0 * lng * _PI) + 20.0 * math.sin(2.0 * lng * _PI))
        * 2.0 / 3.0
    )
    ret += (
        (20.0 * math.sin(lng * _PI) + 40.0 * math.sin(lng / 3.0 * _PI))
        * 2.0 / 3.0
    )
    ret += (
        (150.0 * math.sin(lng / 12.0 * _PI) + 300.0 * math.sin(lng / 30.0 * _PI))
        * 2.0 / 3.0
    )
    return ret


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    """Convert GCJ-02 coordinates to WGS-84.

    Returns ``(lon, lat)`` in WGS-84.
    """
    if _out_of_china(lng, lat):
        return lng, lat

    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _PI
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * _PI)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * _PI)
    return lng - dlng, lat - dlat


# ------------------------------------------------------------------
# AMap geocoding
# ------------------------------------------------------------------

_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"


async def amap_geocode(address: str, api_key: str) -> tuple[float, float]:
    """Resolve *address* to ``(lat, lon)`` in WGS-84 via AMap.

    AMap returns GCJ-02 coordinates; this function applies the
    standard conversion so that downstream weather APIs receive
    accurate WGS-84 positions.

    Raises ``ValueError`` on failure.
    """
    if not api_key:
        raise ValueError("请在插件设置中配置 amap_key")

    async with httpx.AsyncClient() as client:
        res = (
            await client.get(_GEOCODE_URL, params={"address": address, "key": api_key})
        ).json()

    if res.get("status") != "1" or not res.get("geocodes"):
        raise ValueError(f"地名解析失败: {res.get('info', 'unknown error')}")

    location: str = res["geocodes"][0]["location"]
    gcj_lng, gcj_lat = (float(x) for x in location.split(","))

    wgs_lng, wgs_lat = gcj02_to_wgs84(gcj_lng, gcj_lat)
    return wgs_lat, wgs_lng
