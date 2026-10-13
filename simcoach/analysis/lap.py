"""Lap recording and storage.

A :class:`Lap` is a set of numpy arrays indexed by distance along the lap.
:class:`LapBuilder` turns a stream of :class:`Sample` into laps, detects the
start/finish crossing and flags laps that should not be used for coaching
(off-tracks, pit lane, resets, missing data).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from simcoach.telemetry.types import TRK_OFF_TRACK, Sample

CHANNELS = ("dist", "time", "speed", "throttle", "brake", "steer", "gear")


@dataclass
class Lap:
    """Telemetry for (part of) one lap. ``dist`` is metres from the S/F line,
    ``time`` is seconds since the lap started, ``speed`` is m/s."""

    dist: np.ndarray
    time: np.ndarray
    speed: np.ndarray
    throttle: np.ndarray
    brake: np.ndarray
    steer: np.ndarray
    gear: np.ndarray
    lap_number: int = 0
    lap_time: float = 0.0
    valid: bool = True
    invalid_reason: str = ""
    source: str = "live"
    meta: dict = field(default_factory=dict)

    @property
    def length(self) -> float:
        return float(self.dist[-1]) if len(self.dist) else 0.0

    def __len__(self) -> int:
        return len(self.dist)

    def time_at(self, d: float) -> float:
        return float(np.interp(d, self.dist, self.time))

    def speed_at(self, d: float) -> float:
        return float(np.interp(d, self.dist, self.speed))

    def window(self, start: float, end: float) -> "Lap":
        mask = (self.dist >= start) & (self.dist <= end)
        return Lap(**{c: getattr(self, c)[mask] for c in CHANNELS}, lap_number=self.lap_number)

    # ---- persistence -------------------------------------------------
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "lap_number": self.lap_number,
            "lap_time": self.lap_time,
            "valid": self.valid,
            "source": self.source,
            "meta": self.meta,
        }
        with open(path, "wb") as fh:
            np.savez_compressed(fh, **{c: getattr(self, c) for c in CHANNELS}, _meta=json.dumps(meta))

    @classmethod
    def load(cls, path: Path) -> "Lap":
        with np.load(path, allow_pickle=False) as data:
            meta = json.loads(str(data["_meta"]))
            arrays = {c: np.asarray(data[c], dtype=float) for c in CHANNELS}
        return cls(**arrays, lap_number=meta.get("lap_number", 0), lap_time=meta.get("lap_time", 0.0),
                   valid=meta.get("valid", True), source=meta.get("source", "file"), meta=meta.get("meta", {}))

    @classmethod
    def from_arrays(cls, dist, speed, throttle, brake, steer=None, gear=None, time=None, **kw) -> "Lap":
        """Build a lap from raw arrays; sorts by distance and derives time from
        speed when no time channel is available."""
        dist = np.asarray(dist, dtype=float)
        order = np.argsort(dist, kind="stable")
        dist = dist[order]
        n = len(dist)

        def arr(x, default=0.0):
            return np.full(n, default, dtype=float) if x is None else np.asarray(x, dtype=float)[order]

        speed = arr(speed)
        if time is None:
            ds = np.diff(dist, prepend=dist[0])
            v = np.maximum((speed + np.roll(speed, 1)) / 2, 1.0)
            v[0] = max(speed[0], 1.0)
            time = np.cumsum(ds / v)
        else:
            time = arr(time)
            time = time - time[0]
        lap = cls(dist=dist, time=time, speed=speed, throttle=arr(throttle), brake=arr(brake),
                  steer=arr(steer), gear=arr(gear), **kw)
        if not lap.lap_time and n:
            lap.lap_time = float(time[-1])
        return lap


class LapBuilder:
    """Accumulates samples into laps.

    ``on_sample`` returns a completed :class:`Lap` when the car crosses the
    start/finish line, otherwise ``None``. ``current`` gives the lap in progress.
    """

    def __init__(self, track_length_m: float):
        self.track_length = track_length_m
        self._reset_buffers()
        self._lap_number: int | None = None
        self._lap_start_time: float | None = None
        self._last: Sample | None = None
        self._invalid_reason = ""

    def _reset_buffers(self) -> None:
        self._buf: dict[str, list[float]] = {c: [] for c in CHANNELS}

    def set_track_length(self, length_m: float) -> None:
        self.track_length = length_m

    @property
    def lap_started(self) -> bool:
        return self._lap_start_time is not None

    @property
    def invalid_reason(self) -> str:
        return self._invalid_reason

    @property
    def current_dist(self) -> float:
        return self._buf["dist"][-1] if self._buf["dist"] else 0.0

    def current(self) -> Lap | None:
        if not self._buf["dist"]:
            return None
        return Lap(**{c: np.asarray(v, dtype=float) for c, v in self._buf.items()},
                   lap_number=self._lap_number or 0, valid=not self._invalid_reason,
                   invalid_reason=self._invalid_reason)

    def _invalidate(self, reason: str) -> None:
        if not self._invalid_reason:
            self._invalid_reason = reason

    def on_sample(self, s: Sample) -> Lap | None:
        prev = self._last
        self._last = s
        if not s.is_on_track:
            if self._lap_start_time is not None:
                self._invalidate("not on track")
            return None

        completed: Lap | None = None
        crossed = prev is not None and prev.lap_dist_pct > 0.9 and s.lap_dist_pct < 0.1
        if crossed:
            # Interpolate the exact crossing time between the two samples.
            span = (1.0 - prev.lap_dist_pct) + s.lap_dist_pct
            frac = (1.0 - prev.lap_dist_pct) / span if span > 0 else 0.0
            t_cross = prev.session_time + frac * (s.session_time - prev.session_time)
            if self._lap_start_time is not None and self._buf["dist"]:
                completed = self._finish_lap(t_cross)
            self._reset_buffers()
            self._lap_start_time = t_cross
            self._lap_number = s.lap
            self._invalid_reason = ""
        elif prev is not None and self._lap_start_time is not None:
            jump = s.lap_dist_pct - prev.lap_dist_pct
            if jump < -0.02 or jump > 0.05:
                self._invalidate("telemetry jump (reset/tow?)")

        if self._lap_start_time is None:
            return completed

        if s.on_pit_road:
            self._invalidate("pit road")
        if s.track_surface == TRK_OFF_TRACK:
            self._invalidate("off track")

        d = s.lap_dist_pct * self.track_length
        if self._buf["dist"] and d <= self._buf["dist"][-1]:
            return completed  # duplicate/stationary tick
        self._buf["dist"].append(d)
        self._buf["time"].append(s.session_time - self._lap_start_time)
        self._buf["speed"].append(s.speed)
        self._buf["throttle"].append(s.throttle)
        self._buf["brake"].append(s.brake)
        self._buf["steer"].append(s.steer)
        self._buf["gear"].append(s.gear)
        return completed

    def _finish_lap(self, t_cross: float) -> Lap:
        lap = self.current()
        assert lap is not None
        lap.lap_time = t_cross - self._lap_start_time
        # Close the lap at exactly the track length.
        lap.dist = np.append(lap.dist, self.track_length)
        lap.time = np.append(lap.time, lap.lap_time)
        for c in ("speed", "throttle", "brake", "steer", "gear"):
            arr = getattr(lap, c)
            setattr(lap, c, np.append(arr, arr[-1]))
        coverage = len(lap.dist) / max(1.0, lap.lap_time)
        if lap.dist[0] > 0.05 * self.track_length:
            self._invalidate("joined mid-lap")
        elif coverage < 5:  # fewer than ~5 samples per second
            self._invalidate("sparse telemetry")
        lap.valid = not self._invalid_reason
        lap.invalid_reason = self._invalid_reason
        return lap
