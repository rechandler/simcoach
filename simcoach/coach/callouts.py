"""Decides *when* to speak.

Talking to a driver mid-corner is worse than saying nothing, so findings are
queued and only delivered when the driver is on a straight (on throttle, off
the brake, not about to brake), with a minimum gap between messages and no
nagging about the same thing every lap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from simcoach.analysis.compare import Finding
from simcoach.config import CalloutConfig

PRIO_SYSTEM = 0  # lower number = more important
PRIO_ANSWER = 1
PRIO_COACH = 2
PRIO_PRAISE = 3

Say = Callable[[str, int], None]


@dataclass
class _Pending:
    finding: Finding
    created: float


class CalloutScheduler:
    def __init__(self, cfg: CalloutConfig, say: Say):
        self.cfg = cfg
        self.say = say
        self.pending: list[_Pending] = []
        self.last_spoken = -1e9
        self._said: dict[tuple[int, str], int] = {}  # (corner, kind) -> lap number
        self._reminders: dict[int, tuple[str, float]] = {}  # corner -> (text, trigger distance)
        self.spoken_log: list[tuple[float, int, Finding]] = []

    def offer(self, f: Finding, now: float, lap: int) -> None:
        key = (f.corner, f.kind)
        last = self._said.get(key)
        if last is not None and lap - last < self.cfg.repeat_laps:
            return
        self.pending = [p for p in self.pending if p.finding.corner != f.corner]
        self.pending.append(_Pending(f, now))

    def set_reminder(self, corner: int, text: str, trigger_m: float) -> None:
        if self.cfg.pre_corner_reminders and text:
            self._reminders[corner] = (text, trigger_m)

    def clear(self) -> None:
        self.pending.clear()

    def _calm(self, throttle: float, brake: float, dist_to_brake: float | None) -> bool:
        if brake > 0.05 or throttle < 0.6:
            return False
        return dist_to_brake is None or dist_to_brake > self.cfg.quiet_before_brake_m

    def tick(self, now: float, lap: int, dist_m: float, throttle: float, brake: float,
             dist_to_brake: float | None) -> None:
        if not self.cfg.enabled:
            self.pending.clear()
            return

        # Pre-corner reminders fire at a fixed spot, regardless of the gap rule.
        for corner, (text, trig) in list(self._reminders.items()):
            if trig <= dist_m < trig + 60 and now - self.last_spoken > 2.0:
                del self._reminders[corner]
                self._speak(text, PRIO_COACH, now)

        self.pending = [p for p in self.pending if now - p.created <= self.cfg.max_age_s]
        if not self.pending or now - self.last_spoken < self.cfg.min_gap_s:
            return
        if not self._calm(throttle, brake, dist_to_brake):
            return
        coaching = [p for p in self.pending if p.finding.kind != "praise"]
        pool = coaching or self.pending
        best = max(pool, key=lambda p: p.finding.priority)
        self.pending.remove(best)
        f = best.finding
        self._said[(f.corner, f.kind)] = lap
        self.spoken_log.append((now, lap, f))
        self._speak(f.message, PRIO_PRAISE if f.kind == "praise" else PRIO_COACH, now)

    def _speak(self, text: str, prio: int, now: float) -> None:
        self.last_spoken = now
        self.say(text, prio)
