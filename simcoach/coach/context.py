"""Turns engine state into compact text the LLM can reason over."""

from __future__ import annotations

from collections import Counter

from simcoach.analysis.compare import describe_corner
from simcoach.analysis.corners import Corner
from simcoach.coach.engine import fmt_laptime

SYSTEM_PROMPT = """\
You are an expert sim racing driving coach talking to a driver over the radio \
while they drive in iRacing. Your answers are converted to speech.

How to answer:
- One to three short sentences, plain spoken English. No lists, markdown, \
symbols or abbreviations like "T2" or "kph" - say "turn two", "k p h" or \
"miles per hour".
- Lead with the single most important, actionable change. Use concrete \
references from the data: brake point in meters relative to their best lap, \
minimum speed, throttle pickup point, coasting.
- Base claims on the telemetry provided. If the data cannot answer the \
question, say so briefly and give the most likely general technique advice, \
clearly as a suggestion.
- Distances in the data are meters from the start/finish line; "ref" is the \
driver's own reference (best) lap. Positive time deltas mean slower than the \
reference.
- The driver is busy: never ask them to read anything, and keep it short.

Coaching knowledge: late braking that lowers minimum speed or delays throttle \
is a net loss (overslowing the car, missed apex). Early braking with lower \
entry speed leaves time on the table. Long coasting between brake release and \
throttle means a missed opportunity to trail brake. A later throttle pickup \
with a lower exit speed costs time all the way down the next straight, so exit \
matters most before long straights."""


def _corner_line(c: Corner) -> str:
    alias = f" (also: {', '.join(c.aliases)})" if c.aliases else ""
    return f"- {c.name}{alias}: apex at {c.apex_m:.0f}m, segment {c.start_m:.0f}-{c.end_m:.0f}m"


def build_context(snap: dict, focus: Corner | None = None, max_laps: int = 6) -> str:
    session = snap["session"]
    track = snap["track"]
    ref = snap["reference"]
    ref_metrics = snap["ref_metrics"]
    history = snap["history"]
    out: list[str] = []

    if session:
        out.append(f"Session: {session.display}, track length {session.track_length_m:.0f} m, "
                   f"session type {session.session_type or 'unknown'}.")
    valid = [r for r in history if r.lap.valid]
    out.append(f"Laps completed: {len(history)} ({len(valid)} valid). "
               f"Driver is currently on lap {snap['current_lap']} at {snap['current_dist']:.0f} m.")
    if ref is not None:
        out.append(f"Reference lap: {fmt_laptime(ref.lap_time)} (source: {ref.source}).")
    else:
        out.append("No reference lap yet - the driver has not completed a clean lap.")

    if track is None or not track.corners:
        out.append("Corner map not available yet.")
        return "\n".join(out)

    out.append("\nCorners:")
    out.extend(_corner_line(c) for c in track.corners)

    recent = valid[-max_laps:]
    if recent:
        out.append("\nRecent valid laps (lap time, then per-corner delta vs reference):")
        for r in recent:
            deltas = []
            for c in track.corners:
                m, rm = r.metrics.get(c.number), ref_metrics.get(c.number)
                if m and rm:
                    deltas.append(f"{c.name} {m.segment_time - rm.segment_time:+.2f}")
            out.append(f"- Lap {r.lap.lap_number}: {fmt_laptime(r.lap.lap_time)}; " + ", ".join(deltas))

        # Average loss per corner across recent laps = where the time is.
        losses = []
        for c in track.corners:
            rm = ref_metrics.get(c.number)
            vals = [r.metrics[c.number].segment_time - rm.segment_time for r in recent
                    if rm and c.number in r.metrics]
            if vals:
                losses.append((sum(vals) / len(vals), c.name))
        losses.sort(reverse=True)
        out.append("Average time lost per corner over those laps (biggest first): "
                   + ", ".join(f"{n} {v:+.2f}s" for v, n in losses[:6]))

    findings = Counter((f.corner_name, f.kind) for r in history[-max_laps:] for f in r.findings
                       if f.kind not in ("praise", "time_loss"))
    if findings:
        out.append("Recurring issues detected (corner, issue, laps): "
                   + "; ".join(f"{n} {k} x{cnt}" for (n, k), cnt in findings.most_common(8)))

    spoken = snap["spoken"][-5:]
    if spoken:
        out.append("Recent things you already told the driver: "
                   + " | ".join(f.message for _, _, f in spoken))

    corners = [focus] if focus else track.corners
    out.append("\nDetailed corner data" + (f" for {focus.name}" if focus else "") + ":")
    for c in corners:
        rm = ref_metrics.get(c.number)
        if rm:
            out.append(f"{c.name} reference: {describe_corner(rm, None)}")
        for r in (recent[-4:] if focus else recent[-1:]):
            m = r.metrics.get(c.number)
            if m:
                out.append(f"{c.name} lap {r.lap.lap_number}: {describe_corner(m, rm)}")
    return "\n".join(out)
