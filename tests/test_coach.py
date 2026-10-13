from simcoach.analysis.compare import Finding
from simcoach.coach.assistant import Assistant, sentences
from simcoach.coach.callouts import CalloutScheduler
from simcoach.coach.engine import CoachEngine
from simcoach.config import CalloutConfig
from simcoach.llm import NoLLM
from simcoach.storage import Store
from simcoach.telemetry.synthetic import CornerError, session_samples
from simcoach.telemetry.types import TRK_OFF_TRACK


class Recorder:
    def __init__(self):
        self.said: list[tuple[str, int]] = []

    def __call__(self, text, prio=2):
        self.said.append((text, prio))

    def text(self):
        return " | ".join(t for t, _ in self.said)


def run_stint(cfg, track, laps, mutate=None):
    say = Recorder()
    eng = CoachEngine(cfg, Store(cfg.data_dir), say, seed=0, save_laps=False)
    eng.start_session(track.session())
    for s in session_samples(track, laps):
        if mutate:
            mutate(s)
        eng.on_sample(s)
    return eng, say


def test_first_clean_lap_becomes_reference_then_coaching(cfg, track):
    eng, say = run_stint(cfg, track, [None, {2: CornerError(brake_offset_m=22)}])
    assert eng.reference is not None and eng.reference.lap_number == 1
    assert eng.track and len(eng.track.corners) == 7
    assert "That's your reference" in say.text()
    assert any("Turn 2" in t and ("earlier" in t or "sooner" in t) for t, _ in say.said)
    # Reference and corner map persisted for next time.
    assert Store(cfg.data_dir).load_reference(track.session()) is not None
    assert Store(cfg.data_dir).load_track_map(track.session()) is not None


def test_personal_best_replaces_reference(cfg, track):
    eng, say = run_stint(cfg, track, [None, {1: CornerError(apex_speed_kph=5)}])
    assert eng.reference.lap_number == 2
    assert "Personal best" in say.text()


def test_invalid_lap_is_not_used_as_reference(cfg, track):
    def off_track_on_lap_1(s):
        if s.lap == 1 and 0.3 < s.lap_dist_pct < 0.31:
            s.track_surface = TRK_OFF_TRACK

    eng, _ = run_stint(cfg, track, [{1: CornerError(apex_speed_kph=10)}, None], mutate=off_track_on_lap_1)
    assert not eng.history[0].lap.valid
    assert eng.reference.lap_number == 2


def test_reference_reloads_on_next_session(cfg, track):
    run_stint(cfg, track, [None])
    eng, say = run_stint(cfg, track, [{4: CornerError(throttle_delay_m=35)}])
    assert "Reference lap is" in say.said[0][0]
    assert any("Turn 4" in t for t, _ in say.said[1:])


def _finding(corner=2, kind="late_brake", dt=0.2):
    return Finding(corner, f"Turn {corner}", kind, dt, f"msg {corner} {kind}", "")


def test_scheduler_waits_for_a_straight_and_respects_gap():
    say = Recorder()
    sch = CalloutScheduler(CalloutConfig(min_gap_s=6), say)
    sch.offer(_finding(), now=0.0, lap=1)
    sch.tick(0.5, 1, 100, throttle=0.0, brake=0.8, dist_to_brake=500)  # braking: stay quiet
    assert not say.said
    sch.tick(1.0, 1, 150, throttle=1.0, brake=0.0, dist_to_brake=50)  # about to brake: quiet
    assert not say.said
    sch.tick(1.5, 1, 200, throttle=1.0, brake=0.0, dist_to_brake=500)
    assert len(say.said) == 1
    sch.offer(_finding(3), now=2.0, lap=1)
    sch.tick(3.0, 1, 300, throttle=1.0, brake=0.0, dist_to_brake=500)  # within min gap
    assert len(say.said) == 1
    sch.tick(8.0, 1, 400, throttle=1.0, brake=0.0, dist_to_brake=500)
    assert len(say.said) == 2


def test_scheduler_does_not_nag_every_lap():
    say = Recorder()
    sch = CalloutScheduler(CalloutConfig(min_gap_s=0, repeat_laps=2), say)
    for lap in (1, 2, 3):
        sch.offer(_finding(), now=lap * 100.0, lap=lap)
        sch.tick(lap * 100.0 + 1, lap, 0, 1.0, 0.0, None)
    assert len(say.said) == 2  # laps 1 and 3


def test_coaching_beats_praise():
    say = Recorder()
    sch = CalloutScheduler(CalloutConfig(min_gap_s=0), say)
    sch.offer(_finding(1, "praise", -0.3), 0.0, 1)
    sch.offer(_finding(2, "late_brake", 0.1), 0.0, 1)
    sch.tick(1.0, 1, 0, 1.0, 0.0, None)
    assert say.said[0][0] == "msg 2 late_brake"


class FakeLLM:
    name = "fake"

    def __init__(self):
        self.calls = []

    def stream(self, system, messages):
        self.calls.append((system, messages))
        yield "Brake earlier "
        yield "into turn two. Then carry"
        yield " more speed."


def test_assistant_sends_focused_context_to_llm(cfg, track):
    eng, _ = run_stint(cfg, track, [None, {2: CornerError(brake_offset_m=22)}])
    llm = FakeLLM()
    say = Recorder()
    Assistant(eng, llm, say).ask("How can I improve turn 2?")
    assert [t for t, _ in say.said] == ["Brake earlier into turn two.", "Then carry more speed."]
    system, messages = llm.calls[0]
    prompt = messages[-1]["content"]
    assert "Detailed corner data for Turn 2" in prompt
    assert "Turn 2 reference:" in prompt and "brake@" in prompt
    assert "How can I improve turn 2?" in prompt
    # Follow-up questions carry short history, not the telemetry blob.
    a = Assistant(eng, llm, say)
    a.ask("and turn 3?")
    a.ask("thanks")
    hist = llm.calls[-1][1][:-1]
    assert hist[0] == {"role": "user", "content": "and turn 3?"}


def test_assistant_without_llm_uses_findings(cfg, track):
    eng, _ = run_stint(cfg, track, [None, {2: CornerError(brake_offset_m=22)}])
    say = Recorder()
    Assistant(eng, NoLLM(), say).ask("how do I go faster in turn 2")
    assert "Turn 2" in say.text()


def test_voice_commands(cfg, track):
    eng, _ = run_stint(cfg, track, [None])
    say = Recorder()
    a = Assistant(eng, NoLLM(), say)
    a.ask("be quiet please")
    assert eng.cfg.callouts.enabled is False
    a.ask("resume coaching")
    assert eng.cfg.callouts.enabled is True
    a.ask("what was my last lap time")
    assert "Last lap was 1" in say.said[-1][0]


def test_sentence_splitter():
    assert list(sentences(iter(["Hi there. How", " are you? Fine"]))) == ["Hi there.", "How are you?", "Fine"]
