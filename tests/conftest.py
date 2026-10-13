import pytest

from simcoach.analysis.corners import detect_corners
from simcoach.analysis.metrics import lap_metrics
from simcoach.config import Config
from simcoach.telemetry.synthetic import SyntheticTrack


@pytest.fixture
def track():
    return SyntheticTrack()


@pytest.fixture
def ref_lap(track):
    return track.lap()


@pytest.fixture
def track_map(ref_lap):
    return detect_corners(ref_lap, "test")


@pytest.fixture
def ref_metrics(ref_lap, track_map):
    return lap_metrics(ref_lap, track_map)


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.data_dir = tmp_path
    c.llm.provider = "none"
    return c
