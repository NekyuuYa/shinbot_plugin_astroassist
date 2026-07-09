"""Tests for Dapiya tropical cyclone floater helpers."""

from __future__ import annotations

import pytest

from shinbot_plugin_astroassist.dapiya_floater import (
    DapiyaFloaterError,
    DapiyaFloaterStorm,
    normalize_dapiya_product,
    parse_dapiya_active_storms,
    parse_dapiya_piclist,
    resolve_dapiya_storm,
)


_ACTIVE_SAMPLE = """09W.BAVI
97W.INVEST|90C.INVEST
"""


def test_parse_dapiya_active_storms() -> None:
    storms = parse_dapiya_active_storms(_ACTIVE_SAMPLE)

    assert storms == [
        DapiyaFloaterStorm(storm_id="09W", name="BAVI", raw="09W.BAVI", group="meso"),
        DapiyaFloaterStorm(
            storm_id="97W", name="INVEST", raw="97W.INVEST", group="floater"
        ),
        DapiyaFloaterStorm(
            storm_id="90C", name="INVEST", raw="90C.INVEST", group="floater"
        ),
    ]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", "09W"),
        ("09W", "09W"),
        ("09W.BAVI", "09W"),
        ("BAVI", "09W"),
        ("2609", "09W"),
        ("2609 巴威", "09W"),
        ("97W", "97W"),
    ],
)
def test_resolve_dapiya_storm(query: str, expected: str) -> None:
    storm = resolve_dapiya_storm(parse_dapiya_active_storms(_ACTIVE_SAMPLE), query)

    assert storm.storm_id == expected


def test_resolve_dapiya_storm_reports_miss() -> None:
    with pytest.raises(DapiyaFloaterError, match="未匹配"):
        resolve_dapiya_storm(parse_dapiya_active_storms(_ACTIVE_SAMPLE), "不存在")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", "VIS"), ("vis", "VIS"), ("RGB", "RGB"), ("true color", "TRUECOLOR")],
)
def test_normalize_dapiya_product(raw: str, expected: str) -> None:
    assert normalize_dapiya_product(raw) == expected


def test_normalize_dapiya_product_rejects_unexposed_types() -> None:
    with pytest.raises(DapiyaFloaterError, match="可选"):
        normalize_dapiya_product("WV")


def test_parse_dapiya_piclist_extracts_frames_and_time() -> None:
    storm = DapiyaFloaterStorm(storm_id="09W", name="BAVI", raw="09W.BAVI")
    frames = parse_dapiya_piclist(
        "/history/09W/VIS/09W_VIS_20260709162500.png,"
        "/history/09W/VIS/09W_VIS_20260709163000.png",
        storm=storm,
        product="VIS",
    )

    assert len(frames) == 2
    assert (
        frames[-1].url
        == "https://data.dapiya.cn/history/09W/VIS/09W_VIS_20260709163000.png"
    )
    assert frames[-1].time == "2026-07-09 16:30:00"
    assert frames[-1].storm_id == "09W"
    assert frames[-1].name == "BAVI"
    assert frames[-1].product == "VIS"
