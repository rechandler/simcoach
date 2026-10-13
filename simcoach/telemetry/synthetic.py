"""A synthetic track and driver for demos and tests (no iRacing needed).

The track is a list of corners (apex distance + apex speed). A lap is
generated with simple physics: accelerate at a fixed rate, brake at a fixed
rate, and per-corner "mistakes" (late/early braking, low apex speed, late
throttle) can be injected to exercise the coaching logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

import numpy as np

from simcoach.analysis.lap import Lap
from simcoach.telemetry.types import Sample, SessionInfo

DEMO_TRACK = [  # (apex distance m, apex speed km/h)
    (420, 95), (900, 150), (1350, 70), (2050, 120), (2600, 85), (3300, 160), (3800, 60),
]
DEMO_LENGTH = 4200.0
VMAX = 260 / 3.6
ACCEL = 7.0  # m/s^2
DECEL = 14.0  # m/s^2


@dataclass
class CornerError:
    brake_offset_m: float = 0.0  # + = brakes later than ideal
    apex_speed_kph: float = 0.0  # + = faster at apex, - = slower
    throttle_delay_m: float = 0.0  # extra coasting/maintenance before throttle
    brake_scale: float = 1.0  # peak brake pressure multiplier


@dataclass
class SyntheticTrack:
    corners: list[tuple[float, float]] = field(default_factory=lambda: list(DEMO_TRACK))
    length: float = DEMO_LENGTH

    def session(self) -> SessionInfo:
        return SessionInfo(track_id="synthetic", track_name="Simcoach Test Circuit", track_length_m=self.length,
                           car_id="synthetic-gt3", car_name="Synthetic GT3", session_type="Practice")

    def lap(self, errors: dict[int, CornerError] | None = None, step: float = 1.0) -> Lap:
        """Generate one lap. ``errors`` maps corner number (1-based) to mistakes."""
        errors = errors or {}
        d = np.arange(0.0, self.length + step, step)
        n = len(d)
        # Speed limit from each corner: ideal braking (decel) before and accel after the apex.
        v = np.full(n, VMAX)
        throttle = np.ones(n)
        brake = np.zeros(n)
        apex_eff: dict[int, float] = {}
        for k, (apex, vk) in enumerate(self.corners, start=1):
            e = errors.get(k, CornerError())
            off = e.brake_offset_m
            # Braking late overshoots: the apex moves later and the car has to
            # slow down more. Braking early means arriving slow and holding speed.
            a = apex + max(off, 0.0)
            apex_eff[k] = a
            va = max(20.0, vk + e.apex_speed_kph - 0.25 * max(off, 0.0)) / 3.6
            decel = DECEL * min(1.0, e.brake_scale)
            before = np.sqrt(va ** 2 + 2 * decel * np.clip(a + min(off, 0.0) - d, 0, None))
            before[d > a] = np.inf
            hold_end = a + e.throttle_delay_m
            after = np.where(d <= hold_end, va, np.sqrt(va ** 2 + 2 * ACCEL * np.clip(d - hold_end, 0, None)))
            after[d < a] = np.inf
            v = np.minimum(v, np.minimum(before, after))
        # Forward pass: acceleration limit from the start.
        v[0] = min(v[0], 200 / 3.6)
        for i in range(1, n):
            v[i] = min(v[i], np.sqrt(v[i - 1] ** 2 + 2 * ACCEL * step))
        # Pedals from the speed profile.
        dv = np.diff(v, append=v[-1])
        braking = dv < -1e-4
        for k in range(1, len(self.corners) + 1):
            e = errors.get(k, CornerError())
            a = apex_eff[k]
            zone = braking & (d > a - 400) & (d <= a)
            brake[zone] = min(1.0, 0.95 * e.brake_scale)
            hold = (d >= a) & (d <= a + e.throttle_delay_m)
            throttle[hold] = 0.0
        throttle[braking] = 0.0
        partial = (~braking) & (dv > 0) & (v < VMAX * 0.999) & (throttle > 0)
        throttle[partial] = 1.0
        # Time from speed.
        vm = np.maximum((v[1:] + v[:-1]) / 2, 1.0)
        t = np.concatenate([[0.0], np.cumsum(step / vm)])
        lap = Lap(dist=d, time=t, speed=v, throttle=throttle, brake=brake, steer=np.zeros(n),
                  gear=np.full(n, 4.0), lap_time=float(t[-1]), source="synthetic")
        return lap


def lap_to_samples(lap: Lap, lap_number: int, t0: float, track_length: float, hz: float = 60.0) -> Iterator[Sample]:
    """Replay a lap as 60 Hz samples (time-based, like the real SDK)."""
    times = np.arange(0.0, lap.lap_time, 1.0 / hz)
    dists = np.interp(times, lap.time, lap.dist)
    for tt, dd in zip(times, dists):
        yield Sample(
            session_time=t0 + float(tt),
            lap=lap_number,
            lap_dist_pct=float(dd / track_length) % 1.0,
            speed=float(np.interp(dd, lap.dist, lap.speed)),
            throttle=float(np.interp(dd, lap.dist, lap.throttle)),
            brake=float(np.interp(dd, lap.dist, lap.brake)),
            gear=4,
        )


def session_samples(track: SyntheticTrack, laps: list[dict[int, CornerError] | None], hz: float = 60.0) -> Iterator[Sample]:
    """A full stint: a short run-up to the line, then the given laps."""
    t = 0.0
    # Run-up: the last 5% of a lap so the first crossing starts lap 1 cleanly.
    first = track.lap(laps[0] if laps else None)
    start_d = track.length * 0.95
    for s in lap_to_samples(first, 0, 0.0, track.length, hz):
        if s.lap_dist_pct * track.length >= start_d:
            s.session_time -= first.time_at(start_d)
            yield s
    t = first.lap_time - first.time_at(start_d)
    for i, errs in enumerate(laps, start=1):
        lap = track.lap(errs)
        yield from lap_to_samples(lap, i, t, track.length, hz)
        t += lap.lap_time
    # A few ticks past the line to close the final lap.
    for k in range(5):
        yield Sample(session_time=t + k / hz, lap=len(laps) + 1, lap_dist_pct=0.001 * (k + 1),
                     speed=60.0, throttle=1.0, brake=0.0)
