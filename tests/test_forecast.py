"""Tests for AstroAssist forecast data preparation."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from shinbot_plugin_astroassist import forecast


@pytest.mark.asyncio
async def test_fetch_forecast_sets_day_span_fields_on_all_hour_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = (
        datetime.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        + datetime.timedelta(days=1)
    )
    times = [(start + datetime.timedelta(hours=offset)).isoformat() for offset in range(3)]

    async def fake_open_meteo(lat: float, lon: float, days: int) -> dict[str, Any]:
        return {
            "hourly": {
                "time": times,
                "temperature_2m": [12, 13, 14],
                "dew_point_2m": [4, 5, 6],
                "wind_speed_10m": [2, 3, 4],
                "relative_humidity_2m": [50, 60, 70],
                "cloud_cover": [10, 20, 30],
                "cloud_cover_low": [1, 2, 3],
                "cloud_cover_mid": [4, 5, 6],
                "cloud_cover_high": [7, 8, 9],
            },
            "daily": {
                "sunrise": [start.replace(hour=6).isoformat()],
                "sunset": [start.replace(hour=18).isoformat()],
            },
        }

    async def fake_7timer(lat: float, lon: float) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(forecast, "_fetch_open_meteo", fake_open_meteo)
    monkeypatch.setattr(forecast, "_fetch_7timer", fake_7timer)

    render_data = await forecast.fetch_forecast(35.0, 139.0)
    hour_rows = [row for row in render_data.rows if not row["is_transition"]]

    assert hour_rows
    assert all("is_first_of_day" in row for row in hour_rows)
    assert all("day_rowspan" in row for row in hour_rows)
    assert [row["is_first_of_day"] for row in hour_rows] == [True, False, False]
