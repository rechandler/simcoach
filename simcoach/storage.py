"""On-disk storage for reference laps and track maps.

Layout under the data dir (default ~/.simcoach):
    references/<track>__<car>.npz   best lap used as the coaching reference
    tracks/<track>.yaml             corner map (editable)
    sessions/<date>_<track>__<car>/ every lap driven, for later review
"""

from __future__ import annotations

import time
from pathlib import Path

from simcoach.analysis.corners import TrackMap
from simcoach.analysis.lap import Lap
from simcoach.telemetry.types import SessionInfo, slug


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    def reference_path(self, session: SessionInfo) -> Path:
        return self.root / "references" / f"{session.key}.npz"

    def track_map_path(self, session: SessionInfo) -> Path:
        # iRacing TrackIDs are unique per layout, so the id alone is enough.
        return self.root / "tracks" / f"{slug(session.track_id)}.yaml"

    def load_reference(self, session: SessionInfo) -> Lap | None:
        p = self.reference_path(session)
        return Lap.load(p) if p.is_file() else None

    def save_reference(self, session: SessionInfo, lap: Lap) -> Path:
        p = self.reference_path(session)
        lap.save(p)
        return p

    def load_track_map(self, session: SessionInfo) -> TrackMap | None:
        p = self.track_map_path(session)
        return TrackMap.load(p) if p.is_file() else None

    def save_track_map(self, session: SessionInfo, track: TrackMap) -> Path:
        p = self.track_map_path(session)
        track.save(p)
        return p

    def session_dir(self, session: SessionInfo, started: float | None = None) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(started or time.time()))
        return self.root / "sessions" / f"{stamp}_{session.key}"
