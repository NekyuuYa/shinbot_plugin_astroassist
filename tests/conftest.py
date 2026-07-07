"""Test path and ShinBot stubs for the AstroAssist plugin package."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


shinbot_module = types.ModuleType("shinbot")
schema_module = types.ModuleType("shinbot.schema")
elements_module = types.ModuleType("shinbot.schema.elements")


class MessageElement:
    """Small MessageElement stub covering AstroAssist test needs."""

    @classmethod
    def img(cls, src: str, **kwargs: Any) -> dict[str, Any]:
        return {"type": "img", "attrs": {"src": src, **kwargs}}

    @classmethod
    def file(cls, src: str, **kwargs: Any) -> dict[str, Any]:
        return {"type": "file", "attrs": {"src": src, **kwargs}}


elements_module.__dict__["MessageElement"] = MessageElement
sys.modules.setdefault("shinbot", shinbot_module)
sys.modules.setdefault("shinbot.schema", schema_module)
sys.modules.setdefault("shinbot.schema.elements", elements_module)
sys.modules.setdefault("httpx", types.ModuleType("httpx"))
