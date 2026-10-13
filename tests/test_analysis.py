import random

import numpy as np
import pytest

from simcoach.analysis.compare import compare_corner
from simcoach.analysis.corners import TrackMap
from simcoach.analysis.lap import Lap
from simcoach.analysis.metrics import lap_metrics
from simcoach.config import CoachingConfig
from simcoach.telemetry.files import lap_from_csv_text
from simcoach.telemetry.synthetic import DEMO_TRACK, CornerError


def test_detects_every_corner_near_its_apex(track_map):
    assert len(track_map.corners) == len(DEMO_TRACK)
    for c, (apex, _) in zip(track_map.corners, DEMO_TRACK):
        assert abs(c.apex_m - apex) < 15
    # Segments tile the whole lap, so corner deltas sum to the lap delta.
    assert track_map.corners[0].start_m == 0
    assert track_map.corners[-1].end_m == pytest.approx(track_map.length_m)
    for a, b in zip(track_map.corners, track_map.corners[1:]):
        assert a.end_m == b.start_m


def test_corner_deltas_sum_to_lap_delta(track, ref_lap, track_map, ref_metrics):
    lap = track.lap({2: CornerError(brake_offset_m=20), 5: CornerError(apex_speed_kph=-6)})
    m = lap_metrics(lap, track_map)
    total = sum(m[k].segment_time - ref_metrics[k].segment_time for k in m)
    assert total == pytest.approx(lap.lap_time - ref_lap.lap_time, abs=0.01)


@pytest.mark.parametrize("corner,error,kind", [
    (2, CornerError(brake_offset_m=20), "late_brake"),
    (3, CornerError(brake_offset_m=-25), "early_brake"),
    (1, CornerError(apex_speed_kph=-8), "low_min_speed"),
    (4, CornerError(throttle_delay_m=30), "late_throttle"),
])
def test_diagnoses_injected_mistakes(track, track_map, ref_metrics, corner, error, kind):
    lap = track.lap({corner: error})
    cur = lap_metrics(lap, track_map)[corner]
    f = compare_corner(cur, ref_metrics[corner], f"Turn {corner}", CoachingConfig(), rng=random.Random(0))
    assert f is not None and f.kind == kind
    assert f.time_delta > 0
    assert f"Turn {corner}" in f.message


def test_late_brake_message_gives_distance(track, track_map, ref_metrics):
    cur = lap_metrics(track.lap({2: CornerError(brake_offset_m=20)}), track_map)[2]
    f = compare_corner(cur, ref_metrics[2], "Turn 2", CoachingConfig(), rng=random.Random(0))
    assert "meters" in f.message and ("sooner" in f.message or "earlier" in f.message)


def test_clean_lap_says_nothing(track, track_map, ref_metrics):
    m = lap_metrics(track.lap(), track_map)
    for k, cur in m.items():
        assert compare_corner(cur, ref_metrics[k], "x", CoachingConfig()) is None


def test_faster_corner_gets_praise(track, track_map, ref_metrics):
    cur = lap_metrics(track.lap({4: CornerError(apex_speed_kph=6)}), track_map)[4]
    f = compare_corner(cur, ref_metrics[4], "Turn 4", CoachingConfig(), rng=random.Random(0))
    assert f.kind == "praise" and f.time_delta < 0


def test_lap_save_load_roundtrip(tmp_path, ref_lap):
    p = tmp_path / "lap.npz"
    ref_lap.save(p)
    back = Lap.load(p)
    assert back.lap_time == pytest.approx(ref_lap.lap_time)
    np.testing.assert_allclose(back.speed, ref_lap.speed)


def test_track_map_roundtrip_and_lookup(tmp_path, track_map):
    track_map.corners[2].name = "The Hairpin"
    track_map.corners[2].aliases = ("hairpin",)
    p = tmp_path / "t.yaml"
    track_map.save(p)
    tm = TrackMap.load(p)
    assert tm.by_name("how do I improve turn 2?").number == 2
    assert tm.by_name("what about T4").number == 4
    assert tm.by_name("tips for the hairpin").name == "The Hairpin"
    assert tm.by_name("how's my driving") is None


def test_csv_import_handles_units_and_percent_distance(ref_lap):
    # Garage 61-style export: LapDistPct, speed in km/h, pedals 0-100.
    rows = ["LapDistPct,Speed,Throttle,Brake,SteeringWheelAngle"]
    for d, v, t, b in zip(ref_lap.dist[::3], ref_lap.speed[::3], ref_lap.throttle[::3], ref_lap.brake[::3]):
        rows.append(f"{d / ref_lap.length:.6f},{v * 3.6:.2f},{t * 100:.1f},{b * 100:.1f},0")
    lap = lap_from_csv_text("\n".join(rows), track_length_m=ref_lap.length)
    assert lap.length == pytest.approx(ref_lap.length, rel=1e-3)
    assert lap.speed.max() == pytest.approx(ref_lap.speed.max(), rel=1e-2)
    assert lap.brake.max() <= 1.0
    # No time column: lap time is integrated from speed.
    assert lap.lap_time == pytest.approx(ref_lap.lap_time, rel=0.01)
