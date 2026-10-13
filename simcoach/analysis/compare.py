"""Compare a corner against the reference and decide what (if anything) to say.

This is deliberately rule-based rather than LLM-based: callouts must be
instant, deterministic and short, and they must never invent numbers.
The LLM is used for open questions, where it gets these same numbers as
context.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from simcoach.analysis.metrics import CornerMetrics
from simcoach.config import CoachingConfig


@dataclass
class Finding:
    corner: int
    corner_name: str
    kind: str  # late_brake, early_brake, light_brake, low_min_speed, late_throttle, coasting, slow_exit, time_loss, praise
    time_delta: float  # + means slower than reference
    message: str  # full sentence, said after the corner
    reminder: str  # very short cue, said before the corner next lap
    magnitude: float = 0.0  # metres / km/h etc. depending on kind

    @property
    def priority(self) -> float:
        return abs(self.time_delta)


def _round_to(x: float, step: float) -> int:
    return int(max(step, round(x / step) * step))


def _dist(m: float, units: str) -> str:
    if units == "imperial":
        ft = m * 3.28084
        return f"{_round_to(ft, 10)} feet"
    return f"{_round_to(m, 5)} meters"


def _speed(kph: float, units: str) -> str:
    if units == "imperial":
        return f"{max(1, round(kph / 1.609))} mph"
    return f"{max(1, round(kph))} k p h"


def _tenths(dt: float) -> str:
    t = abs(dt)
    if t < 0.095:
        return "a few hundredths"
    tenths = round(t * 10)
    if tenths == 1:
        return "a tenth"
    if tenths < 10:
        return f"{tenths} tenths"
    return f"{t:.1f} seconds"


def compare_corner(cur: CornerMetrics, ref: CornerMetrics, name: str, cfg: CoachingConfig,
                   units: str = "metric", rng: random.Random | None = None) -> Finding | None:
    """Return the single most useful finding for this corner, or None."""
    rng = rng or random
    dt = cur.segment_time - ref.segment_time

    if dt <= -cfg.praise_gain_s and cfg.praise_enabled:
        msg = rng.choice([
            f"Nice {name}, {_tenths(dt)} up.",
            f"Good {name}, that's {_tenths(dt)} quicker.",
            f"{name} was strong, {_tenths(dt)} gained.",
        ])
        return Finding(cur.corner, name, "praise", dt, msg, "", -dt)

    if dt < cfg.min_time_loss_s:
        return None

    lost = _tenths(dt)
    cands: list[tuple[float, Finding]] = []

    def add(kind, mag, message, reminder, weight):
        f = Finding(cur.corner, name, kind, dt, message, reminder, mag)
        cands.append((weight, f))

    min_dv = ref.min_speed_kph - cur.min_speed_kph  # + means slower than ref
    exit_dv = ref.exit_speed_kph - cur.exit_speed_kph

    if cur.brake_point_m is not None and ref.brake_point_m is not None:
        db = cur.brake_point_m - ref.brake_point_m  # + means braked later
        if db >= cfg.late_brake_m and (min_dv > 1.5 or exit_dv > 1.5 or (cur.throttle_pickup_m or 0) > (ref.throttle_pickup_m or 0)):
            add("late_brake", db,
                rng.choice([
                    f"Braking too late into {name}, you overshot and lost {lost}. Try braking about {_dist(db, units)} sooner next lap.",
                    f"{name}: late on the brakes, it cost {lost} on exit. Brake around {_dist(db, units)} earlier.",
                ]),
                f"{name}, brake a bit earlier.", db / 4 + min_dv)
        elif -db >= cfg.early_brake_m:
            add("early_brake", -db,
                rng.choice([
                    f"You're braking early for {name}. You can brake about {_dist(-db, units)} later.",
                    f"{name}: there's time on entry, try braking {_dist(-db, units)} deeper.",
                ]),
                f"{name}, brake later.", -db / 4)

    if (cur.brake_point_m is not None and ref.peak_brake - cur.peak_brake >= cfg.brake_pressure
            and abs((cur.brake_point_m or 0) - (ref.brake_point_m or 0)) < cfg.late_brake_m * 1.5):
        add("light_brake", (ref.peak_brake - cur.peak_brake) * 100,
            f"Hit the brakes harder at the start of {name}, you're at {cur.peak_brake * 100:.0f} percent "
            f"versus {ref.peak_brake * 100:.0f} on your best lap.",
            f"{name}, brake hard.", (ref.peak_brake - cur.peak_brake) * 40)

    if min_dv >= cfg.min_speed_kph:
        trail_more = (cur.brake_release_m is not None and ref.brake_release_m is not None
                      and cur.brake_release_m - ref.brake_release_m > 10)
        hint = " Release the brake a little earlier." if trail_more else " Roll more speed to the apex."
        add("low_min_speed", min_dv,
            f"{name}: minimum speed is {_speed(min_dv, units)} down on your best.{hint}",
            f"{name}, carry more speed.", min_dv)

    if (cur.throttle_pickup_m is not None and ref.throttle_pickup_m is not None
            and cur.throttle_pickup_m - ref.throttle_pickup_m >= cfg.throttle_late_m):
        dd = cur.throttle_pickup_m - ref.throttle_pickup_m
        add("late_throttle", dd,
            rng.choice([
                f"Get back to throttle earlier out of {name}, you were {_dist(dd, units)} late picking it up.",
                f"{name}: throttle pickup is {_dist(dd, units)} later than your best. Commit earlier.",
            ]),
            f"{name}, early throttle.", dd / 4)

    if cur.coast_m - ref.coast_m >= cfg.coast_m:
        dd = cur.coast_m - ref.coast_m
        add("coasting", dd,
            f"Too much coasting in {name}, about {_dist(dd, units)} with no pedal. Trail the brake into the throttle.",
            f"{name}, no coasting.", dd / 5)

    if exit_dv >= cfg.exit_speed_kph and not cands:
        add("slow_exit", exit_dv,
            f"Exit of {name} is {_speed(exit_dv, units)} slow. Sacrifice a little entry for a better exit.",
            f"{name}, focus on exit.", exit_dv)

    if not cands:
        return Finding(cur.corner, name, "time_loss", dt, f"Lost {lost} in {name}.", "", dt)
    cands.sort(key=lambda wf: wf[0], reverse=True)
    return cands[0][1]


def describe_corner(cur: CornerMetrics, ref: CornerMetrics | None) -> str:
    """Compact, factual description used as LLM context."""
    def fmt(x, nd=0):
        return "-" if x is None else f"{x:.{nd}f}"

    parts = [
        f"time {cur.segment_time:.2f}s",
        f"brake@{fmt(cur.brake_point_m)}m",
        f"peak brake {cur.peak_brake * 100:.0f}%",
        f"entry {cur.entry_speed_kph:.0f}kph",
        f"min {cur.min_speed_kph:.0f}kph@{cur.min_speed_m:.0f}m",
        f"throttle@{fmt(cur.throttle_pickup_m)}m",
        f"full@{fmt(cur.full_throttle_m)}m",
        f"coast {cur.coast_m:.0f}m",
        f"exit {cur.exit_speed_kph:.0f}kph",
    ]
    s = ", ".join(parts)
    if ref is not None:
        s += f" | vs ref {cur.segment_time - ref.segment_time:+.2f}s"
    return s
