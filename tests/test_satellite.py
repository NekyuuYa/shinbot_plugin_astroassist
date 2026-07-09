"""Tests for NMC sea-area cloud image parsing and command wiring."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shinbot_plugin_astroassist.commands import register_commands
from shinbot_plugin_astroassist.satellite import (
    _parse_frames,
    _parse_latest,
    resolve_satellite_page,
)
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


_NMC_SATELLITE_HTML = """
<img id="imgpath"
  data-index="0"
  data-time="07/09 23:00"
  src="https://image.nmc.cn/product/2026/07/09/WXSP/medium/latest.png?v=1">
<div class="col-xs-12 time actived"
  data-index="0"
  data-img="https://image.nmc.cn/product/2026/07/09/WXSP/medium/latest.png?v=1"
  data-time="07/09 23:00"><div> 07/09 23:00 </div></div>
<div class="col-xs-12 time"
  data-index="1"
  data-img="https://image.nmc.cn/product/2026/07/09/WXSP/medium/older.png?v=2"
  data-time="07/09 22:00"><div> 07/09 22:00 </div></div>
"""

_NMC_SATELLITE_DUPLICATE_TIME_HTML = """
<img id="imgpath"
  src="https://image.nmc.cn/product/2026/07/09/WXSP/medium/latest.png?v=1">
<div class="col-xs-12 time actived"
  data-img="https://image.nmc.cn/product/2026/07/09/WXSP/medium/latest.png?v=1"
  data-time="07/09 23:00"></div>
"""


def _register(tmp_path: Path) -> _FakePlugin:
    plugin = _FakePlugin(tmp_path)
    register_commands(
        plugin,
        SimpleNamespace(amap_key=""),
        LocationStore(tmp_path),
        tmp_path / "template.html",
    )
    return plugin


def test_parse_nmc_satellite_html_extracts_latest_and_frames() -> None:
    latest_url, latest_time = _parse_latest(_NMC_SATELLITE_HTML)
    frames = _parse_frames(_NMC_SATELLITE_HTML)

    assert latest_url == "https://image.nmc.cn/product/2026/07/09/WXSP/medium/latest.png?v=1"
    assert latest_time == "07/09 23:00"
    assert frames == [
        {
            "url": "https://image.nmc.cn/product/2026/07/09/WXSP/medium/latest.png?v=1",
            "time": "07/09 23:00",
        },
        {
            "url": "https://image.nmc.cn/product/2026/07/09/WXSP/medium/older.png?v=2",
            "time": "07/09 22:00",
        },
    ]


def test_parse_nmc_satellite_html_fills_time_from_duplicate_timeline() -> None:
    assert _parse_latest(_NMC_SATELLITE_DUPLICATE_TIME_HTML) == (
        "https://image.nmc.cn/product/2026/07/09/WXSP/medium/latest.png?v=1",
        "07/09 23:00",
    )


def test_resolve_satellite_page_defaults_to_nmc_sea_infrared_product() -> None:
    assert resolve_satellite_page("") == (
        "/publish/satellite/China_Northwest_Pacific_Ocean.html",
        "海区红外云图",
    )
    assert resolve_satellite_page("西北太平洋") == (
        "/publish/satellite/China_Northwest_Pacific_Ocean.html",
        "西北太平洋海区红外云图",
    )


def test_register_commands_declares_sea_cloud_commands(tmp_path: Path) -> None:
    plugin = _register(tmp_path)

    assert "卫星云图" not in plugin.commands
    assert "卫星云图动图" not in plugin.commands
    assert plugin.commands["海区云图"]["aliases"] == ["seacloud", "sea"]
    assert plugin.commands["海区云图动图"]["aliases"] == ["seacloudgif", "seagif"]


@pytest.mark.asyncio
async def test_radar_gif_command_sends_normal_image_subtype(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shinbot_plugin_astroassist.commands as commands

    async def fake_fetch_gif(query: str) -> tuple[bytes, str, str, str]:
        assert query == ""
        return b"gif", "23:00", "22:00", "全国"

    monkeypatch.setattr(commands, "fetch_radar_gif", fake_fetch_gif)

    plugin = _register(tmp_path)
    ctx = _Ctx()
    await plugin.commands["雷达动图"]["handler"](ctx, "")

    assert ctx.stopped is True
    image = ctx.sent[1][0]
    assert image["type"] == "img"
    assert image["attrs"]["sub_type"] == "0"


@pytest.mark.asyncio
async def test_sea_cloud_command_sends_static_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shinbot_plugin_astroassist.commands as commands

    async def fake_fetch(query: str) -> tuple[str, str, str]:
        assert query == "西北太平洋"
        return (
            "https://image.nmc.cn/product/2026/07/09/WXSP/medium/latest.png?v=1",
            "07/09 23:00",
            "西北太平洋海区红外云图",
        )

    async def fake_download(url: str, dest: Path) -> None:
        assert "latest.png" in url
        dest.write_bytes(b"png")

    monkeypatch.setattr(commands, "fetch_satellite", fake_fetch)
    monkeypatch.setattr(commands, "download_satellite_image", fake_download)

    plugin = _register(tmp_path)
    ctx = _Ctx()
    await plugin.commands["海区云图"]["handler"](ctx, "西北太平洋")

    assert ctx.stopped is True
    assert "西北太平洋海区红外云图" in ctx.sent[0]
    assert ctx.sent[1][0]["type"] == "img"
    assert Path(ctx.sent[1][0]["attrs"]["src"]).name.startswith("sea_cloud_latest_")


@pytest.mark.asyncio
async def test_sea_cloud_gif_command_sends_normal_image_subtype(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shinbot_plugin_astroassist.commands as commands

    async def fake_fetch_gif(query: str) -> tuple[bytes, str, str, str]:
        assert query == ""
        return b"gif", "07/09 23:00", "07/09 22:00", "海区红外云图"

    monkeypatch.setattr(commands, "fetch_satellite_gif", fake_fetch_gif)

    plugin = _register(tmp_path)
    ctx = _Ctx()
    await plugin.commands["海区云图动图"]["handler"](ctx, "")

    assert ctx.stopped is True
    assert "07/09 22:00 → 07/09 23:00" in ctx.sent[0]
    image = ctx.sent[1][0]
    assert image["type"] == "img"
    assert image["attrs"]["sub_type"] == "0"
    assert Path(image["attrs"]["src"]).name.startswith("sea_cloud_animated_")
