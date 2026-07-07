"""File-based key-value store for location persistence."""

from __future__ import annotations

import json
from pathlib import Path

from .models import LocationData


class LocationStore:
    """JSON-file-backed location storage under *data_dir* / locations/."""

    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir / "locations"
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(self, key: str) -> LocationData | None:
        """Return the stored location for *key*, or ``None``."""
        path = self._path(key)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return LocationData.from_dict(raw)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    async def put(self, key: str, location: LocationData) -> None:
        """Persist *location* under *key*."""
        path = self._path(key)
        path.write_text(
            json.dumps(location.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path(self, key: str) -> Path:
        # Sanitize key so it's safe as a filename
        safe = key.replace("/", "_").replace("\\", "_").replace(":", "_")
        return self._dir / f"{safe}.json"
