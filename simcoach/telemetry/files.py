"""Load laps from files: iRacing .ibt telemetry and CSV exports (Garage 61,
MoTeC/SimHub exports, or anything with distance + speed + pedals)."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Iterable, Iterator

import numpy as np

from simcoach.analysis.lap import Lap
from simcoach.telemetry.types import Sample

# Column aliases, compared case-insensitively with spaces/underscores removed.
ALIASES = {
    "pct": ["lapdistpct", "distpct", "lapdistancepct", "distancepct", "lapdist%", "pct"],
    "dist": ["lapdist", "distance", "lapdistance", "dist", "distancem"],
    "time": ["laptime", "lapcurrentlaptime", "time", "elapsedtime", "sessiontime", "t"],
    "speed": ["speed", "speedms", "speedkph", "speedkmh", "groundspeed", "velocity"],
    "throttle": ["throttle", "throttleraw", "gas", "accelerator"],
    "brake": ["brake", "brakeraw", "brakepressure"],
    "steer": ["steeringwheelangle", "steering", "steer", "steeringangle"],
    "gear": ["gear"],
    "lap": ["lap", "lapnumber"],
}


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum() or ch == "%")


def _find(columns: dict[str, str], key: str) -> str | None:
    for alias in ALIASES[key]:
        if alias in columns:
            return columns[alias]
    return None


def lap_from_csv_text(text: str, track_length_m: float | None = None, source: str = "csv") -> Lap:
    """Parse a single-lap CSV. Speed may be m/s or km/h, pedals 0-1 or 0-100.

    Needs either a distance column (metres) or a lap-distance-percentage
    column plus ``track_length_m``.
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    columns = {_norm(c): c for c in reader.fieldnames}
    rows = list(reader)
    if not rows:
        raise ValueError("CSV has no data rows")

    def col(key: str) -> np.ndarray | None:
        name = _find(columns, key)
        if name is None:
            return None
        vals = []
        for r in rows:
            try:
                vals.append(float(r[name]))
            except (TypeError, ValueError):
                vals.append(np.nan)
        arr = np.asarray(vals, dtype=float)
        return arr

    speed = col("speed")
    throttle = col("throttle")
    brake = col("brake")
    if speed is None or throttle is None or brake is None:
        raise ValueError(f"CSV needs speed, throttle and brake columns; found {reader.fieldnames}")

    dist = col("dist")
    pct = col("pct")
    if pct is not None and np.nanmax(pct) > 1.5:
        pct = pct / 100.0
    if dist is None:
        if pct is None:
            raise ValueError("CSV needs a distance or lap-distance-percentage column")
        if not track_length_m:
            # Integrate speed over time as a last resort.
            t = col("time")
            if t is None:
                raise ValueError("Pass --track-length: CSV only has percentage distance")
            v = speed if np.nanmax(speed) < 110 else speed / 3.6
            track_length_m = float(np.nansum(np.diff(t, prepend=t[0]) * v))
        dist = pct * track_length_m

    good = ~(np.isnan(dist) | np.isnan(speed))
    if np.nanmax(speed) > 110:  # nothing laps at >396 km/h, so this must be km/h
        speed = speed / 3.6
    for name, arr in (("throttle", throttle), ("brake", brake)):
        if np.nanmax(arr) > 1.5:
            arr /= 100.0
    time = col("time")
    if time is not None and (np.isnan(time).any() or np.any(np.diff(time[good]) < 0)):
        time = None

    sel = lambda a: None if a is None else np.nan_to_num(a[good])  # noqa: E731
    return Lap.from_arrays(dist=dist[good], speed=speed[good], throttle=sel(throttle), brake=sel(brake),
                           steer=sel(col("steer")), gear=sel(col("gear")), time=sel(time), source=source)


def lap_from_csv(path: Path, track_length_m: float | None = None) -> Lap:
    return lap_from_csv_text(Path(path).read_text(encoding="utf-8-sig"), track_length_m, source=f"csv:{Path(path).name}")


def ibt_samples(path: Path) -> Iterator[Sample]:
    """Stream samples from an iRacing .ibt file (needs pyirsdk)."""
    try:
        import irsdk
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("pip install pyirsdk to read .ibt files") from e
    ibt = irsdk.IBT()
    ibt.open(str(path))
    try:
        names = set(ibt.var_headers_names or [])

        def get(name, default=0.0):
            return ibt.get_all(name) if name in names else None

        chans = {k: get(k) for k in ("SessionTime", "Lap", "LapDistPct", "Speed", "Throttle", "Brake",
                                     "SteeringWheelAngle", "Gear", "RPM", "OnPitRoad", "PlayerTrackSurface",
                                     "IsOnTrack")}
        n = len(chans["SessionTime"])
        for i in range(n):
            def v(k, d=0):
                arr = chans[k]
                return arr[i] if arr is not None else d

            yield Sample(
                session_time=float(v("SessionTime")), lap=int(v("Lap")), lap_dist_pct=float(v("LapDistPct")),
                speed=float(v("Speed")), throttle=float(v("Throttle")), brake=float(v("Brake")),
                steer=float(v("SteeringWheelAngle")), gear=int(v("Gear")), rpm=float(v("RPM")),
                on_pit_road=bool(v("OnPitRoad", False)), track_surface=int(v("PlayerTrackSurface", 3)),
                is_on_track=bool(v("IsOnTrack", True)),
            )
    finally:
        ibt.close()


def ibt_track_length(path: Path) -> float:
    """Estimate track length from the LapDist channel of an .ibt file."""
    import irsdk

    ibt = irsdk.IBT()
    ibt.open(str(path))
    try:
        dist = np.asarray(ibt.get_all("LapDist"), dtype=float)
        pct = np.asarray(ibt.get_all("LapDistPct"), dtype=float)
    finally:
        ibt.close()
    mask = pct > 0.5
    return float(np.median(dist[mask] / pct[mask])) if mask.any() else float(dist.max())


def laps_from_samples(samples: Iterable[Sample], track_length_m: float) -> list[Lap]:
    from simcoach.analysis.lap import LapBuilder

    b = LapBuilder(track_length_m)
    laps = []
    for s in samples:
        lap = b.on_sample(s)
        if lap is not None:
            laps.append(lap)
    return laps


def best_lap(laps: list[Lap]) -> Lap | None:
    valid = [lap for lap in laps if lap.valid]
    return min(valid, key=lambda lap: lap.lap_time) if valid else None
