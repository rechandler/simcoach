"""Telemetry data types shared by every source (live iRacing, files, synthetic)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# iRacing irsdk_TrkLoc values
TRK_NOT_IN_WORLD = -1
TRK_OFF_TRACK = 0
TRK_IN_PIT_STALL = 1
TRK_APPROACHING_PITS = 2
TRK_ON_TRACK = 3


@dataclass(slots=True)
class Sample:
    """One telemetry tick for the player's car."""

    session_time: float  # seconds
    lap: int
    lap_dist_pct: float  # 0..1
    speed: float  # m/s
    throttle: float  # 0..1
    brake: float  # 0..1
    steer: float = 0.0  # radians, steering wheel angle
    gear: int = 0
    rpm: float = 0.0
    on_pit_road: bool = False
    track_surface: int = TRK_ON_TRACK
    is_on_track: bool = True


@dataclass
class SessionInfo:
    track_id: str = "unknown"
    track_name: str = "Unknown track"
    track_config: str = ""
    track_length_m: float = 0.0
    car_id: str = "unknown"
    car_name: str = "Unknown car"
    session_type: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Filesystem-safe key identifying a track/car combination."""
        return f"{slug(self.track_id)}__{slug(self.car_id)}"

    @property
    def display(self) -> str:
        cfg = f" ({self.track_config})" if self.track_config else ""
        return f"{self.car_name} @ {self.track_name}{cfg}"


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(text)).strip("-").lower() or "unknown"


def parse_track_length(value: str | float | None) -> float:
    """iRacing reports e.g. '5.51 km' or '3.42 mi'. Returns metres."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    m = re.match(r"\s*([\d.]+)\s*(km|mi|m)?", value)
    if not m:
        return 0.0
    num = float(m.group(1))
    unit = (m.group(2) or "km").lower()
    return num * {"km": 1000.0, "mi": 1609.344, "m": 1.0}[unit]
