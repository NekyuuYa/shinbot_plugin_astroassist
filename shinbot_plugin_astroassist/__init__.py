"""AstroAssist — 天文气象看板插件 for ShinBot."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from .commands import register_commands
from .storage import LocationStore

if TYPE_CHECKING:
    from shinbot.core.plugins.context import Plugin

_LOG = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Plugin metadata
# ------------------------------------------------------------------

__plugin_name__ = "晴天钟助手"
__plugin_description__ = (
    "调用 Open-Meteo 获取 ECMWF 云量数据，支持高德地图定位、专业天文指标与日夜主题切换。"
)


# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------


class AstroAssistConfig(BaseModel):
    """Plugin configuration (loaded from config.toml under [plugins.config])."""

    amap_key: str = ""
    """高德地图 Web服务 API Key (用于地名解析)。"""

    auto_theme: bool = True
    """根据当地日出日落自动切换深浅色看板。"""


__plugin_config_class__ = AstroAssistConfig  # noqa: N816


# ------------------------------------------------------------------
# Setup / lifecycle
# ------------------------------------------------------------------


def setup(plg: Plugin) -> None:
    """Plugin entry point — called once at load time."""
    # Load config
    config = _load_config(plg, AstroAssistConfig)

    # Storage
    store = LocationStore(plg.data_dir)

    # Template path (bundled alongside this module)
    template_path = Path(__file__).parent / "template.html"

    # Register commands
    register_commands(plg, config, store, template_path)

    # Probe RenderKit availability
    try:
        from shinbot_plugin_renderkit import probe_renderkit_capabilities  # noqa: F401

        caps = probe_renderkit_capabilities()
        if caps.html:
            _LOG.info("RenderKit HTML backend available — rendering enabled.")
        else:
            _LOG.warning("RenderKit installed but HTML backend unavailable. Install playwright.")
    except ImportError:
        _LOG.warning(
            "RenderKit not found — image rendering disabled. "
            "Install shinbot_plugin_renderkit for PNG output."
        )

    _LOG.info("AstroAssist loaded (amap_key=%s).", "set" if config.amap_key else "NOT SET")


async def on_disable(_plg: Plugin) -> None:
    """Cleanup on plugin disable."""


# ------------------------------------------------------------------
# Config loader helper
# ------------------------------------------------------------------


def _load_config(plg: Plugin, cls: type[AstroAssistConfig]) -> AstroAssistConfig:
    """Load plugin config from the main TOML config file."""
    import tomllib

    from shinbot.core.application.paths import DEFAULT_CONFIG_PATH
    from shinbot.core.plugins.config import plugin_config_block

    raw: dict[str, object] = {}
    try:
        path = DEFAULT_CONFIG_PATH
        if path.exists():
            with path.open("rb") as f:
                payload = tomllib.load(f)
            raw = plugin_config_block(payload, plg.plugin_id)
    except Exception:
        raw = {}

    try:
        return cls.model_validate(raw)
    except Exception:
        return cls()
