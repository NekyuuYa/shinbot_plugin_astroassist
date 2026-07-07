"""Pydantic data models for AstroAssist forecast rendering."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class LocationData:
    """Persisted location reference."""

    lat: float
    lon: float
    name: str

    def to_dict(self) -> dict[str, float | str]:
        return {"lat": self.lat, "lon": self.lon, "name": self.name}

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> LocationData:
        return cls(lat=float(d["lat"]), lon=float(d["lon"]), name=str(d["name"]))  # type: ignore[arg-type]


@dataclass(slots=True)
class RowData:
    """One hourly forecast row for template rendering."""

    is_transition: bool = False
    day: str = ""
    hour: str = ""

    temp_val: int = 0
    temp_color: str = ""
    temp_cls: str = ""

    dew_val: int = 0
    dew_color: str = ""
    dew_cls: str = ""

    humi_val: int = 0
    humi_color: str = ""
    humi_cls: str = ""

    wind_val: int = 0
    wind_color: str = ""
    wind_cls: str = ""

    seeing_val: int | str = "/"
    seeing_color: str = ""
    seeing_cls: str = ""

    trans_val: int | str = "/"
    trans_color: str = ""
    trans_cls: str = ""

    total: int = 0
    low: int = 0
    mid: int = 0
    high: int = 0

    # Set during day-span calculation
    is_first_of_day: bool = False
    day_rowspan: int = 0


@dataclass(slots=True)
class TransitionRow:
    """Sunrise / sunset separator row."""

    is_transition: bool = True
    label: str = ""
    day: str = ""


@dataclass(slots=True)
class RenderData:
    """Fully prepared data bundle passed to the Jinja2 template."""

    lat: float
    lon: float
    location_name: str
    ref_time: str
    rows: list[dict[str, object]] = field(default_factory=list)
    theme_mode: str = "light-mode"
    model_name: str = "ECMWF+7Timer"
