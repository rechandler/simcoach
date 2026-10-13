"""The coaching engine: telemetry in, spoken feedback out.

Runs on the telemetry thread. Thread-safe accessors are provided for the voice
assistant (which builds LLM context from the engine's lap history).
"""

from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass, field

from simcoach.analysis.compare import Finding, compare_corner
from simcoach.analysis.corners import TrackMap, detect_corners
from simcoach.analysis.lap import Lap, LapBuilder
from simcoach.analysis.metrics import CornerMetrics, corner_metrics, lap_metrics
from simcoach.coach.callouts import PRIO_SYSTEM, CalloutScheduler, Say
from simcoach.config import Config
from simcoach.storage import Store
from simcoach.telemetry.types import Sample, SessionInfo

log = logging.getLogger(__name__)


def fmt_laptime(t: float) -> str:
    if not t or t <= 0:
        return "-"
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:06.3f}" if m else f"{s:.3f}"


def spoken_laptime(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m)} {s:04.1f}" if m else f"{s:.1f}"


@dataclass
class LapRecord:
    lap: Lap
    metrics: dict[int, CornerMetrics]
    findings: list[Finding] = field(default_factory=list)
    ref_time: float | None = None  # reference lap time at the moment this lap was driven


class CoachEngine:
    def __init__(self, cfg: Config, store: Store, say: Say, seed: int | None = None,
                 save_laps: bool = True):
        self.cfg = cfg
        self.store = store
        self.say = say
        self.rng = random.Random(seed)
        self.save_laps = save_laps
        self.lock = threading.RLock()
        self.scheduler = CalloutScheduler(cfg.callouts, say)
        self.session: SessionInfo | None = None
        self.builder: LapBuilder | None = None
        self.reference: Lap | None = None
        self.ref_metrics: dict[int, CornerMetrics] = {}
        self.track: TrackMap | None = None
        self.history: list[LapRecord] = []
        self._analyzed: set[int] = set()
        self._cur_findings: list[Finding] = []
        self._last_sample: Sample | None = None
        self._session_dir = None

    # ------------------------------------------------------------------ setup
    def start_session(self, session: SessionInfo) -> None:
        with self.lock:
            self.session = session
            self.builder = LapBuilder(session.track_length_m)
            self.history.clear()
            self.scheduler.clear()
            self._analyzed.clear()
            self._session_dir = self.store.session_dir(session) if self.save_laps else None
            self.reference = self.store.load_reference(session)
            self.track = self.store.load_track_map(session)
            if self.reference is not None and self.track is None:
                self._build_track_map(self.reference)
            self._refresh_ref_metrics()
            log.info("Session: %s (key %s)", session.display, session.key)
            if self.reference is not None:
                self.say(f"Coach ready. Reference lap is {spoken_laptime(self.reference.lap_time)}.", PRIO_SYSTEM)
            else:
                self.say("Coach ready. Drive a clean lap and I'll use it as your reference.", PRIO_SYSTEM)

    def set_reference(self, lap: Lap, announce: bool = True) -> None:
        with self.lock:
            assert self.session is not None
            self.reference = lap
            self.store.save_reference(self.session, lap)
            if self.track is None:
                self._build_track_map(lap)
            self._refresh_ref_metrics()
            if announce:
                self._announce(f"New reference, {spoken_laptime(lap.lap_time)}.")

    def _announce(self, text: str) -> None:
        """Important messages bypass the queue but still count toward the gap."""
        if self._last_sample is not None:
            self.scheduler.last_spoken = self._last_sample.session_time
        self.say(text, PRIO_SYSTEM)

    def _build_track_map(self, lap: Lap) -> None:
        assert self.session is not None
        self.track = detect_corners(lap, track_key=self.session.key)
        path = self.store.save_track_map(self.session, self.track)
        log.info("Detected %d corners, saved to %s", len(self.track.corners), path)

    def _refresh_ref_metrics(self) -> None:
        self.ref_metrics = lap_metrics(self.reference, self.track) if (self.reference and self.track) else {}

    # ------------------------------------------------------------- telemetry
    def on_sample(self, s: Sample) -> None:
        with self.lock:
            if self.builder is None:
                return
            self._last_sample = s
            done = self.builder.on_sample(s)
            if done is not None:
                self._finish_lap(done, s)
            if self.track and self.ref_metrics and self.builder.lap_started:
                self._analyze_finished_corners(s)
            dist = self.builder.current_dist
            self.scheduler.tick(s.session_time, s.lap, dist, s.throttle, s.brake, self._dist_to_next_brake(dist))

    def _dist_to_next_brake(self, dist: float) -> float | None:
        if not self.ref_metrics or not self.track:
            return None
        points = sorted(m.brake_point_m for m in self.ref_metrics.values() if m.brake_point_m is not None)
        if not points:
            return None
        for p in points:
            if p > dist:
                return p - dist
        return points[0] + self.track.length_m - dist

    def _analyze_finished_corners(self, s: Sample) -> None:
        assert self.builder and self.track
        if self.builder.invalid_reason:
            return
        dist = self.builder.current_dist
        lap = None
        for c in self.track.corners:
            if c.number in self._analyzed or c.end_m > dist - 5 or c.end_m >= self.track.length_m - 1:
                continue
            self._analyzed.add(c.number)
            lap = lap or self.builder.current()
            self._evaluate_corner(lap, c, s)

    def _evaluate_corner(self, lap: Lap, corner, s: Sample) -> None:
        ref = self.ref_metrics.get(corner.number)
        cur = corner_metrics(lap, corner, self.track)
        if ref is None or cur is None:
            return
        f = compare_corner(cur, ref, corner.name, self.cfg.coaching, self.cfg.units, self.rng)
        if f is None:
            return
        self._cur_findings.append(f)
        log.debug("T%d %s %+.2fs: %s", corner.number, f.kind, f.time_delta, f.message)
        if f.kind != "time_loss":
            self.scheduler.offer(f, s.session_time, s.lap)
        if f.reminder and ref.brake_point_m is not None:
            lead = self.cfg.callouts.reminder_lead_s * max(ref.entry_speed_kph / 3.6, 20.0)
            trig = ref.brake_point_m - lead
            if trig < 0:
                trig += self.track.length_m
            self.scheduler.set_reminder(corner.number, f.reminder, trig)

    def _finish_lap(self, lap: Lap, s: Sample) -> None:
        # The last corner ends at the line, so it can only be judged now.
        if self.track and self.ref_metrics and lap.valid:
            for c in self.track.corners:
                if c.number not in self._analyzed:
                    self._evaluate_corner(lap, c, s)
        metrics = lap_metrics(lap, self.track) if self.track else {}
        rec = LapRecord(lap, metrics, list(self._cur_findings),
                        self.reference.lap_time if self.reference else None)
        self.history.append(rec)
        self._cur_findings.clear()
        self._analyzed.clear()
        if self._session_dir is not None:
            try:
                lap.save(self._session_dir / f"lap_{len(self.history):03d}.npz")
            except OSError:
                log.exception("could not save lap")

        status = "valid" if lap.valid else f"invalid: {lap.invalid_reason}"
        delta = f" ({lap.lap_time - rec.ref_time:+.3f} vs ref)" if rec.ref_time else ""
        log.info("Lap %d: %s%s [%s]", lap.lap_number, fmt_laptime(lap.lap_time), delta, status)

        if not lap.valid:
            return
        if self.reference is None:
            self.set_reference(lap, announce=False)
            n = len(self.track.corners) if self.track else 0
            self._announce(f"Lap time {spoken_laptime(lap.lap_time)}. That's your reference. "
                           f"I found {n} corners, coaching starts now.")
        elif lap.lap_time < self.reference.lap_time - 1e-3:
            gain = self.reference.lap_time - lap.lap_time
            self.set_reference(lap, announce=False)
            self._announce(f"Personal best! {spoken_laptime(lap.lap_time)}, {gain:.1f} quicker.")

    # ------------------------------------------------------- queries (Q&A)
    def snapshot(self) -> dict:
        """A consistent copy of the state needed to answer questions."""
        with self.lock:
            return {
                "session": self.session,
                "track": self.track,
                "reference": self.reference,
                "ref_metrics": dict(self.ref_metrics),
                "history": list(self.history),
                "spoken": list(self.scheduler.spoken_log),
                "current_dist": self.builder.current_dist if self.builder else 0.0,
                "current_lap": self._last_sample.lap if self._last_sample else 0,
            }
