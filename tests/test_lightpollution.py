"""Tests for DarkMap light pollution fetching and Bortle classification."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shinbot_plugin_astroassist import lightpollution
from shinbot_plugin_astroassist.commands import register_commands
from shinbot_plugin_astroassist.lightpollution import (
    LightPollution,
    classify_bortle,
    fetch_light_pollution,
    format_light_pollution_report,
)
from shinbot_plugin_astroassist.models import LocationData
from shinbot_plugin_astroassist.storage import LocationStore


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


def _register(tmp_path: Path, *, amap_key: str = "") -> _FakePlugin:
    plugin = _FakePlugin(tmp_path)
    register_commands(
        plugin,
        SimpleNamespace(amap_key=amap_key),
        LocationStore(tmp_path),
        tmp_path / "template.html",
    )
    return plugin


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict[str, Any], **_: Any) -> None:
        self._payload = payload
        self.requested: tuple[float, float] | None = None

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def get(self, url: str, *, params: dict[str, Any] | None = None) -> _FakeResponse:
        assert url == lightpollution._URL
        self.requested = (params["lat"], params["lon"])
        return _FakeResponse(self._payload)


@pytest.mark.parametrize(
    ("mpsas", "expected"),
    [
        (22.0, 1),
        (21.75, 1),
        (21.74, 2),
        (21.60, 2),
        (21.59, 3),
        (21.30, 3),
        (21.29, 4),
        (20.49, 4),
        (20.48, 5),
        (19.50, 5),
        (19.49, 6),
        (18.94, 6),
        (18.93, 7),
        (18.38, 7),
        (18.37, 8),
        (17.80, 8),
        (17.79, 9),
        (10.0, 9),
    ],
)
def test_classify_bortle_thresholds(mpsas: float, expected: int) -> None:
    assert classify_bortle(mpsas) == expected


@pytest.mark.asyncio
async def test_fetch_light_pollution_parses_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        {
            "latitude": 35.069,
            "longitude": 116.959,
            "dataVersion": "2025.v1",
            "brightness": {"mpsas": 20.553, "ratio": 2.791},
        }
    )
    # conftest stubs httpx as an empty module; create AsyncClient on it
    monkeypatch.setattr(
        lightpollution.httpx, "AsyncClient", lambda **kw: client, raising=False
    )

    result = await fetch_light_pollution(35.069, 116.959)

    assert client.requested == (35.069, 116.959)
    assert result == LightPollution(
        mpsas=20.553,
        ratio=2.791,
        bortle=4,
        label="乡村/城郊过渡",
        data_version="2025.v1",
    )
    assert result.bortle_text == "Bortle 4 · 乡村/城郊过渡"
    assert result.info.nelm == "6.1–6.5"


@pytest.mark.asyncio
async def test_fetch_light_pollution_rejects_invalid_coordinates() -> None:
    with pytest.raises(ValueError, match="无效坐标"):
        await fetch_light_pollution(91.0, 116.0)
    with pytest.raises(ValueError, match="无效坐标"):
        await fetch_light_pollution(35.0, 181.0)


@pytest.mark.asyncio
async def test_fetch_light_pollution_rejects_missing_brightness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lightpollution.httpx,
        "AsyncClient",
        lambda **kw: _FakeClient({"brightness": {}}),
        raising=False,
    )

    with pytest.raises(ValueError, match="缺少亮度数据"):
        await fetch_light_pollution(35.0, 116.0)


@pytest.mark.asyncio
async def test_fetch_light_pollution_rejects_implausible_brightness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lightpollution.httpx,
        "AsyncClient",
        lambda **kw: _FakeClient({"brightness": {"mpsas": 99.0, "ratio": -1}}),
        raising=False,
    )

    with pytest.raises(ValueError, match="无效的亮度数据"):
        await fetch_light_pollution(35.0, 116.0)


def test_format_light_pollution_report() -> None:
    lp = LightPollution(
        mpsas=20.553,
        ratio=2.791,
        bortle=4,
        label="乡村/城郊过渡",
        data_version="2025.v1",
    )

    text = format_light_pollution_report("济宁", 35.069, 116.959, lp)

    assert "光污染报告 | 济宁" in text
    assert "📍 35.0690, 116.9590" in text
    assert "Bortle 4 · 乡村/城郊过渡" in text
    assert "天光背景 (SQM)：20.55 mag/arcsec²" in text
    assert "相对自然天光：2.79 ×" in text
    assert "极限星等 (NELM)：约 6.1–6.5 等" in text
    assert "银河可见性" in text
    assert "观测建议" in text
    assert "数据源：DarkMap 2025.v1" in text


def test_register_commands_declares_light_pollution_command(tmp_path: Path) -> None:
    plugin = _register(tmp_path)

    assert "光污染" in plugin.commands
    assert plugin.commands["光污染"]["aliases"] == ["lightpollution", "bortle", "光害"]
    assert "地名" in plugin.commands["光污染"]["usage"]


@pytest.mark.asyncio
async def test_light_pollution_command_reports_stored_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shinbot_plugin_astroassist.commands as commands

    store = LocationStore(tmp_path)
    await store.put(
        "session-1",
        LocationData(lat=35.069, lon=116.959, name="济宁"),
    )

    async def fake_fetch(lat: float, lon: float) -> LightPollution:
        assert (lat, lon) == (35.069, 116.959)
        return LightPollution(
            mpsas=20.553,
            ratio=2.791,
            bortle=4,
            label="乡村/城郊过渡",
            data_version="2025.v1",
        )

    monkeypatch.setattr(commands, "fetch_light_pollution", fake_fetch)
    plugin = _register(tmp_path)
    ctx = _Ctx()

    await plugin.commands["光污染"]["handler"](ctx, "")

    assert ctx.stopped
    assert "光污染报告 | 济宁" in ctx.sent[-1]
    assert "Bortle 4 · 乡村/城郊过渡" in ctx.sent[-1]


@pytest.mark.asyncio
async def test_light_pollution_command_uses_temporary_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shinbot_plugin_astroassist.commands as commands

    async def fake_geocode(place: str, key: str) -> tuple[float, float]:
        assert place == "西藏阿里"
        return (32.5, 80.1)

    async def fake_fetch(lat: float, lon: float) -> LightPollution:
        assert (lat, lon) == (32.5, 80.1)
        return LightPollution(
            mpsas=21.89,
            ratio=1.2,
            bortle=2,
            label="典型暗夜",
            data_version="2025.v1",
        )

    monkeypatch.setattr(commands, "amap_geocode", fake_geocode)
    monkeypatch.setattr(commands, "fetch_light_pollution", fake_fetch)
    plugin = _register(tmp_path, amap_key="test-key")
    ctx = _Ctx()

    await plugin.commands["光污染"]["handler"](ctx, "西藏阿里")

    assert ctx.stopped
    assert "光污染报告 | 西藏阿里" in ctx.sent[-1]
    assert "Bortle 2 · 典型暗夜" in ctx.sent[-1]


@pytest.mark.asyncio
async def test_light_pollution_command_requires_location(tmp_path: Path) -> None:
    plugin = _register(tmp_path)
    ctx = _Ctx()

    await plugin.commands["光污染"]["handler"](ctx, "")

    assert ctx.stopped
    assert "设置位置" in ctx.sent[-1]


@pytest.mark.asyncio
async def test_light_pollution_command_help(tmp_path: Path) -> None:
    plugin = _register(tmp_path)
    ctx = _Ctx()

    await plugin.commands["光污染"]["handler"](ctx, "help")

    assert ctx.stopped
    assert "光污染报告 | 说明" in ctx.sent[-1]
