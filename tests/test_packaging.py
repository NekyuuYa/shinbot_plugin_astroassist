"""Packaging metadata tests for AstroAssist."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_pyproject_declares_playwright_runtime_dependency() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    dependencies = payload["project"]["dependencies"]
    assert any(item.startswith("playwright") for item in dependencies)
