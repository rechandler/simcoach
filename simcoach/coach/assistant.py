"""Answers the driver's spoken questions using the LLM and the engine state."""

from __future__ import annotations

import logging
import re
import threading
from typing import Iterator

from simcoach.coach.callouts import PRIO_ANSWER, Say
from simcoach.coach.context import SYSTEM_PROMPT, build_context
from simcoach.coach.engine import CoachEngine, fmt_laptime, spoken_laptime
from simcoach.llm import LLM, NoLLM

log = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def sentences(chunks: Iterator[str]) -> Iterator[str]:
    """Regroup streamed text into whole sentences so TTS can start early."""
    buf = ""
    for chunk in chunks:
        buf += chunk
        parts = _SENTENCE_END.split(buf)
        for s in parts[:-1]:
            if s.strip():
                yield s.strip()
        buf = parts[-1]
    if buf.strip():
        yield buf.strip()


class Assistant:
    def __init__(self, engine: CoachEngine, llm: LLM, say: Say, memory_turns: int = 3):
        self.engine = engine
        self.llm = llm
        self.say = say
        self.memory_turns = memory_turns
        self._turns: list[dict] = []
        self._lock = threading.Lock()

    # Simple commands handled without the LLM: instant and free.
    def _local(self, q: str) -> str | None:
        ql = q.lower()
        snap = self.engine.snapshot()
        if re.search(r"\b(be quiet|shut up|stop talking|mute|silence)\b", ql):
            self.engine.cfg.callouts.enabled = False
            return "Okay, going quiet. Say resume coaching to turn me back on."
        if re.search(r"\b(resume|start|turn on) (coaching|callouts|talking)\b", ql):
            self.engine.cfg.callouts.enabled = True
            return "Coaching back on."
        if re.search(r"\b(last|previous) lap\b", ql) and "time" in ql:
            hist = snap["history"]
            if not hist:
                return "No completed laps yet."
            lap = hist[-1].lap
            return f"Last lap was {spoken_laptime(lap.lap_time)}" + ("." if lap.valid else ", but it was invalid.")
        if re.search(r"\b(best|fastest|reference) lap\b", ql) and "time" in ql:
            ref = snap["reference"]
            return f"Your best is {spoken_laptime(ref.lap_time)}." if ref else "No reference lap yet."
        return None

    def answer_text(self, question: str) -> Iterator[str]:
        """Stream the answer as sentences (does not speak)."""
        local = self._local(question)
        if local:
            yield local
            return
        snap = self.engine.snapshot()
        track = snap["track"]
        focus = track.by_name(question) if track else None
        if isinstance(self.llm, NoLLM):
            yield from self._fallback_answer(snap, focus)
            return
        context = build_context(snap, focus)
        user = f"<telemetry>\n{context}\n</telemetry>\n\nDriver asks: {question}"
        with self._lock:
            messages = self._turns[-2 * self.memory_turns:] + [{"role": "user", "content": user}]
        reply = []
        try:
            for s in sentences(self.llm.stream(SYSTEM_PROMPT, messages)):
                reply.append(s)
                yield s
        except Exception:
            log.exception("LLM request failed")
            yield "Sorry, I couldn't reach the coach model."
            yield from self._fallback_answer(snap, focus)
            return
        with self._lock:
            # Store the question without the bulky telemetry to keep later turns short.
            self._turns += [{"role": "user", "content": question},
                            {"role": "assistant", "content": " ".join(reply) or "(no answer)"}]

    def _fallback_answer(self, snap: dict, focus) -> Iterator[str]:
        """Rule-based answer used when no LLM is configured or it fails."""
        hist = [r for r in snap["history"] if r.lap.valid]
        track, refm = snap["track"], snap["ref_metrics"]
        if focus is None and track and refm and hist:
            # "Where am I losing time?" -> corner with the biggest average loss.
            recent = hist[-3:]
            losses = []
            for c in track.corners:
                vals = [r.metrics[c.number].segment_time - refm[c.number].segment_time
                        for r in recent if c.number in r.metrics and c.number in refm]
                if vals:
                    losses.append((sum(vals) / len(vals), c))
            if losses:
                loss, worst = max(losses, key=lambda x: x[0])
                if loss > 0.03:
                    yield f"Your biggest loss is {worst.name}, about {loss:.2f} seconds a lap."
                    focus = worst
        for r in reversed(hist):
            for f in r.findings:
                if f.kind in ("praise", "time_loss"):
                    continue
                if focus is None or f.corner == focus.number:
                    yield f.message
                    return
        if focus:
            yield f"Nothing specific on {focus.name} yet, it's close to your reference."
        else:
            ref = snap["reference"]
            yield f"No clear issues yet. Your reference is {fmt_laptime(ref.lap_time) if ref else 'not set'}."

    def ask(self, question: str) -> None:
        """Answer out loud. Safe to call from any thread."""
        log.info("Driver: %s", question)
        for s in self.answer_text(question):
            log.info("Coach: %s", s)
            self.say(s, PRIO_ANSWER)
