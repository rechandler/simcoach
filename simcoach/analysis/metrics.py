"""Per-corner driving metrics."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from simcoach.analysis.corners import Corner, TrackMap
from simcoach.analysis.lap import Lap

BRAKE_ON = 0.10
BRAKE_OFF = 0.05
THROTTLE_PICKUP = 0.20
THROTTLE_FULL = 0.90
PEDAL_IDLE = 0.05


@dataclass
class CornerMetrics:
    corner: int
    segment_time: float  # seconds spent in the corner's segment
    brake_point_m: float | None  # distance where braking started (None = flat out)
    brake_release_m: float | None  # last braking before the minimum speed point
    peak_brake: float  # 0..1
    entry_speed_kph: float  # speed at the brake point (or segment start)
    min_speed_kph: float
    min_speed_m: float
    throttle_pickup_m: float | None  # first meaningful throttle after min speed
    full_throttle_m: float | None
    coast_m: float  # distance with neither pedal in the corner phase
    exit_speed_kph: float  # speed at segment end

    def as_dict(self) -> dict:
        return asdict(self)


def _first_sustained(d: np.ndarray, mask: np.ndarray, min_len_m: float = 10.0) -> int | None:
    """First index where ``mask`` turns on and stays on for ``min_len_m``
    (ignores brief throttle stabs and noise)."""
    idx = np.flatnonzero(mask)
    if not len(idx):
        return None
    # Split into contiguous runs.
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate([[idx[0]], idx[breaks + 1]])
    ends = np.concatenate([idx[breaks], [idx[-1]]])
    for a, b in zip(starts, ends):
        if d[b] - d[a] >= min_len_m or b == len(d) - 1:
            return int(a)
    return None


def corner_metrics(lap: Lap, corner: Corner, track: TrackMap) -> CornerMetrics | None:
    """Compute metrics for ``corner`` on ``lap``. ``lap`` may be a lap in
    progress as long as it already covers the corner segment."""
    if len(lap) < 10 or lap.dist[0] > corner.start_m + 5 or lap.dist[-1] < corner.end_m - 5:
        return None

    d, v, thr, brk = lap.dist, lap.speed * 3.6, lap.throttle, lap.brake
    idx = {c.number: i for i, c in enumerate(track.corners)}[corner.number]
    prev_apex = track.corners[idx - 1].apex_m if idx > 0 else 0.0
    next_apex = track.corners[idx + 1].apex_m if idx + 1 < len(track.corners) else track.length_m

    seg = (d >= corner.start_m) & (d <= corner.end_m)
    seg_time = lap.time_at(min(corner.end_m, d[-1])) - lap.time_at(max(corner.start_m, d[0]))

    # Minimum speed near the apex (stay close so we don't catch the neighbour).
    near = seg & (np.abs(d - corner.apex_m) <= 120)
    if not near.any():
        near = seg
    near_idx = np.flatnonzero(near)
    i_min = int(near_idx[np.argmin(v[near])])

    # Braking zone: walk back from the minimum-speed point while the brake is on.
    brake_point = brake_release = None
    peak_brake = 0.0
    zone = np.flatnonzero((d >= prev_apex) & (d <= d[i_min]) & (brk >= BRAKE_ON))
    if len(zone):
        # Last contiguous braking run before the apex.
        end = zone[-1]
        start = end
        for j in zone[::-1]:
            if d[start] - d[j] > 15.0:  # allow brief dips in pressure
                break
            start = j
        brake_point = float(d[start])
        brake_release = float(d[end])
        peak_brake = float(brk[start: end + 1].max())

    i_entry = int(np.searchsorted(d, brake_point)) if brake_point is not None else int(np.flatnonzero(seg)[0])
    entry_speed = float(v[min(i_entry, len(v) - 1)])

    after = (d > d[i_min]) & (d <= next_apex)
    j = _first_sustained(d, after & (thr >= THROTTLE_PICKUP))
    pickup = float(d[j]) if j is not None else None
    j = _first_sustained(d, after & (thr >= THROTTLE_FULL))
    full = float(d[j]) if j is not None else None

    phase_start = brake_release if brake_release is not None else float(d[i_min]) - 50
    phase_end = pickup if pickup is not None else corner.end_m
    phase = (d >= phase_start) & (d <= phase_end) & (thr < PEDAL_IDLE) & (brk < PEDAL_IDLE)
    coast = float(np.sum(np.diff(d, prepend=d[0])[phase]))

    return CornerMetrics(
        corner=corner.number,
        segment_time=float(seg_time),
        brake_point_m=brake_point,
        brake_release_m=brake_release,
        peak_brake=peak_brake,
        entry_speed_kph=entry_speed,
        min_speed_kph=float(v[i_min]),
        min_speed_m=float(d[i_min]),
        throttle_pickup_m=pickup,
        full_throttle_m=full,
        coast_m=coast,
        exit_speed_kph=float(np.interp(corner.end_m, d, v)),
    )


def lap_metrics(lap: Lap, track: TrackMap) -> dict[int, CornerMetrics]:
    out = {}
    for c in track.corners:
        m = corner_metrics(lap, c, track)
        if m is not None:
            out[c.number] = m
    return out
