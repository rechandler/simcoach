"""Command line interface: ``simcoach <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

from simcoach.config import Config, load_config
from simcoach.storage import Store
from simcoach.telemetry.types import SessionInfo

log = logging.getLogger("simcoach")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "anthropic", "faster_whisper", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _speaker(cfg: Config, console: bool):
    from simcoach.voice.tts import ConsoleSpeaker, make_speaker

    return ConsoleSpeaker() if console else make_speaker(cfg.voice)


def _llm(cfg: Config):
    from simcoach.llm import NoLLM, make_llm

    try:
        llm = make_llm(cfg.llm)
        log.info("Coach model: %s", llm.name)
        return llm
    except Exception as e:
        log.warning("LLM unavailable (%s) - questions get rule-based answers only", e)
        return NoLLM()


def _stdin_questions(assistant) -> None:
    """Type questions in the terminal (handy without a mic, or for testing)."""
    def loop():
        for line in sys.stdin:
            q = line.strip()
            if q:
                threading.Thread(target=assistant.ask, args=(q,), daemon=True).start()

    threading.Thread(target=loop, name="stdin", daemon=True).start()


# --------------------------------------------------------------------- run
def cmd_run(cfg: Config, args) -> int:
    from simcoach.coach.assistant import Assistant
    from simcoach.coach.engine import CoachEngine
    from simcoach.telemetry.iracing import IRacingSource

    speaker = _speaker(cfg, args.console)
    engine = CoachEngine(cfg, Store(cfg.data_dir), speaker.say)
    assistant = Assistant(engine, _llm(cfg), speaker.say)

    if cfg.overlay.enabled:
        from simcoach.integrations.overlay import serve

        serve(engine, cfg.overlay.host, cfg.overlay.port)
        log.info("Overlay at http://%s:%d/", cfg.overlay.host, cfg.overlay.port)

    voice = None
    if cfg.voice.ptt != "none" and not args.no_voice:
        try:
            from simcoach.voice.loop import VoiceLoop

            voice = VoiceLoop(cfg.voice, assistant, speaker)
            voice.start()
        except Exception as e:
            log.warning("Voice input disabled: %s. Type questions here instead.", e)
    _stdin_questions(assistant)

    src = IRacingSource()
    while True:
        src.wait_for_connection()
        log.info("Connected to iRacing")
        for sample in src.samples():
            if src.session_changed():
                info = src.session_info()
                if engine.session is None or info.key != engine.session.key:
                    log.info("Track/car keys for reference import: --track-id %s --car-id %s",
                             info.track_id, info.car_id)
                    engine.start_session(info)
                    if voice:
                        voice.update_vocabulary()
            engine.on_sample(sample)
        log.info("iRacing disconnected")
        time.sleep(2)


# -------------------------------------------------------------------- demo
def _offline_run(cfg: Config, args, session: SessionInfo, samples, realtime: bool) -> int:
    from simcoach.coach.assistant import Assistant
    from simcoach.coach.engine import CoachEngine

    speaker = _speaker(cfg, console=not realtime or args.console)
    engine = CoachEngine(cfg, Store(cfg.data_dir), speaker.say, seed=1, save_laps=False)
    assistant = Assistant(engine, _llm(cfg), speaker.say)
    engine.start_session(session)
    t0 = time.monotonic()
    first = None
    for s in samples:
        if realtime:
            first = s.session_time if first is None else first
            delay = (s.session_time - first) - (time.monotonic() - t0)
            if delay > 0:
                time.sleep(delay)
        engine.on_sample(s)
        if not realtime:
            speaker.wait_idle(5)
    speaker.wait_idle()
    _print_summary(engine)
    for q in args.ask or []:
        print(f"\n[YOU] {q}")
        assistant.ask(q)
        speaker.wait_idle(60)
    return 0


def _print_summary(engine) -> None:
    from simcoach.coach.engine import fmt_laptime

    snap = engine.snapshot()
    track, refm = snap["track"], snap["ref_metrics"]
    print("\nLap summary (per-corner delta vs reference, seconds):")
    names = [c.name.replace("Turn ", "T") for c in track.corners] if track else []
    print(f"{'lap':>4} {'time':>9}  " + " ".join(f"{n:>6}" for n in names))
    for r in snap["history"]:
        cells = []
        for c in track.corners if track else []:
            m, rm = r.metrics.get(c.number), refm.get(c.number)
            cells.append(f"{m.segment_time - rm.segment_time:+6.2f}" if m and rm else f"{'-':>6}")
        flag = "" if r.lap.valid else f"  invalid ({r.lap.invalid_reason})"
        print(f"{r.lap.lap_number:>4} {fmt_laptime(r.lap.lap_time):>9}  " + " ".join(cells) + flag)


def cmd_demo(cfg: Config, args) -> int:
    """Simulated stint on a synthetic track with typical mistakes injected."""
    import tempfile

    from simcoach.telemetry.synthetic import CornerError as E
    from simcoach.telemetry.synthetic import SyntheticTrack, session_samples

    track = SyntheticTrack()
    stint = [
        None,  # clean lap -> becomes the reference
        {2: E(brake_offset_m=22), 5: E(throttle_delay_m=30)},
        {2: E(brake_offset_m=18), 3: E(apex_speed_kph=-9)},
        {3: E(brake_offset_m=-30), 6: E(brake_scale=0.75)},
        {1: E(apex_speed_kph=4), 4: E(apex_speed_kph=5)},  # faster: new best
        {7: E(throttle_delay_m=40)},
    ]
    if not args.keep_data:
        cfg.data_dir = Path(tempfile.mkdtemp(prefix="simcoach-demo-"))
    if args.ask is None:
        args.ask = ["How can I improve turn 2?", "Where am I losing the most time?"]
    return _offline_run(cfg, args, track.session(), session_samples(track, stint), args.realtime)


def cmd_replay(cfg: Config, args) -> int:
    """Run the coach over a recorded .ibt file."""
    from simcoach.telemetry.files import ibt_samples, ibt_track_length

    path = Path(args.file)
    length = args.track_length or ibt_track_length(path)
    session = SessionInfo(track_id=args.track_id or path.stem, track_name=args.track_id or path.stem,
                          track_length_m=length, car_id=args.car_id or "unknown", car_name=args.car_id or "car")
    return _offline_run(cfg, args, session, ibt_samples(path), args.realtime)


# --------------------------------------------------------------- references
def _session_for(args) -> SessionInfo:
    return SessionInfo(track_id=args.track_id, car_id=args.car_id, track_length_m=args.track_length or 0.0)


def _save_reference(cfg: Config, session: SessionInfo, lap) -> None:
    from simcoach.analysis.corners import detect_corners
    from simcoach.coach.engine import fmt_laptime

    store = Store(cfg.data_dir)
    p = store.save_reference(session, lap)
    print(f"Saved reference {fmt_laptime(lap.lap_time)} ({lap.source}) -> {p}")
    if store.load_track_map(session) is None:
        tm = detect_corners(lap, session.key)
        print(f"Detected {len(tm.corners)} corners -> {store.save_track_map(session, tm)}")


def cmd_ref_import(cfg: Config, args) -> int:
    from simcoach.telemetry.files import best_lap, ibt_samples, ibt_track_length, lap_from_csv, laps_from_samples

    path = Path(args.file)
    if path.suffix.lower() == ".ibt":
        length = args.track_length or ibt_track_length(path)
        lap = best_lap(laps_from_samples(ibt_samples(path), length))
        if lap is None:
            print("No valid complete lap found in that .ibt file")
            return 1
        lap.source = f"ibt:{path.name}"
    else:
        lap = lap_from_csv(path, args.track_length)
    _save_reference(cfg, _session_for(args), lap)
    return 0


def cmd_g61_laps(cfg: Config, args) -> int:
    from simcoach.integrations.garage61 import Garage61, describe_lap

    g = Garage61(cfg.garage61.token)
    if args.list_tracks:
        for t in g.tracks():
            print(t.get("id"), t.get("name"), t.get("variant", ""))
        return 0
    if args.list_cars:
        for c in g.cars():
            print(c.get("id"), c.get("name"))
        return 0
    drivers = None if args.drivers == "team" else args.drivers
    for item in g.find_laps(track=args.track, car=args.car, drivers=drivers, limit=args.limit):
        print(describe_lap(item))
    return 0


def cmd_g61_import(cfg: Config, args) -> int:
    from simcoach.integrations.garage61 import Garage61

    lap = Garage61(cfg.garage61.token).lap_telemetry(args.lap_id, args.track_length)
    _save_reference(cfg, _session_for(args), lap)
    return 0


# ------------------------------------------------------------------ devices
def cmd_devices(cfg: Config, args) -> int:
    try:
        from simcoach.voice.ptt import list_devices, watch_buttons

        devs = list_devices()
        print("Game controllers:" if devs else "No game controllers found.")
        for name, n in devs:
            print(f"  {name}  ({n} buttons)")
        if devs:
            watch_buttons(args.seconds)
    except ImportError:
        print("pip install 'simcoach[voice]' first")
    try:
        import sounddevice as sd

        print("\nMicrophones:")
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                print(f"  [{i}] {d['name']}")
    except ImportError:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="simcoach", description="Live AI driving coach for iRacing")
    p.add_argument("-c", "--config", help="YAML config file (default: ./simcoach.yaml or ~/.simcoach/config.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="coach live in iRacing")
    r.add_argument("--no-voice", action="store_true", help="no push-to-talk (type questions instead)")
    r.add_argument("--console", action="store_true", help="print callouts instead of speaking them")
    r.set_defaults(func=cmd_run)

    def offline(sp):
        sp.add_argument("--realtime", action="store_true", help="play back at real speed with voice")
        sp.add_argument("--console", action="store_true", help="print instead of speaking (with --realtime)")
        sp.add_argument("--ask", action="append", help="question to ask at the end (repeatable)")

    d = sub.add_parser("demo", help="simulated session on a synthetic track (no iRacing needed)")
    offline(d)
    d.add_argument("--keep-data", action="store_true", help="use the real data dir instead of a temp dir")
    d.set_defaults(func=cmd_demo)

    rp = sub.add_parser("replay", help="run the coach over a recorded .ibt telemetry file")
    rp.add_argument("file")
    rp.add_argument("--track-id")
    rp.add_argument("--car-id")
    rp.add_argument("--track-length", type=float)
    offline(rp)
    rp.set_defaults(func=cmd_replay)

    ref = sub.add_parser("reference", help="manage reference laps").add_subparsers(dest="refcmd", required=True)
    ri = ref.add_parser("import", help="import a reference lap from .ibt or .csv")
    ri.add_argument("file")
    for sp in (ri,):
        sp.add_argument("--track-id", required=True, help="iRacing TrackID (printed by `simcoach run`)")
        sp.add_argument("--car-id", required=True, help="iRacing CarPath (printed by `simcoach run`)")
        sp.add_argument("--track-length", type=float, help="metres (needed for CSVs with only LapDistPct)")
    ri.set_defaults(func=cmd_ref_import)

    g = sub.add_parser("garage61", aliases=["g61"], help="Garage 61 integration").add_subparsers(dest="gcmd", required=True)
    gl = g.add_parser("laps", help="list best laps for a track/car")
    gl.add_argument("--track", type=int, help="Garage 61 track id")
    gl.add_argument("--car", type=int, help="Garage 61 car id")
    gl.add_argument("--drivers", default="me", choices=["me", "following", "team"])
    gl.add_argument("--limit", type=int, default=20)
    gl.add_argument("--list-tracks", action="store_true")
    gl.add_argument("--list-cars", action="store_true")
    gl.set_defaults(func=cmd_g61_laps)
    gi = g.add_parser("import", help="use a Garage 61 lap as your reference")
    gi.add_argument("lap_id")
    gi.add_argument("--track-id", required=True, help="iRacing TrackID (printed by `simcoach run`)")
    gi.add_argument("--car-id", required=True, help="iRacing CarPath (printed by `simcoach run`)")
    gi.add_argument("--track-length", type=float, help="metres (if the CSV has only LapDistPct)")
    gi.set_defaults(func=cmd_g61_import)

    dv = sub.add_parser("devices", help="find your wheel's push-to-talk button and microphone")
    dv.add_argument("--seconds", type=float, default=20)
    dv.set_defaults(func=cmd_devices)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = load_config(args.config)
    try:
        return args.func(cfg, args) or 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
