"""Live telemetry from the iRacing SDK (shared memory, 60 Hz) via pyirsdk."""

from __future__ import annotations

import logging
import time
from typing import Iterator

from simcoach.telemetry.types import TRK_ON_TRACK, Sample, SessionInfo, parse_track_length

log = logging.getLogger(__name__)


class IRacingSource:
    def __init__(self, hz: float = 60.0):
        try:
            import irsdk
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("pip install 'simcoach[iracing]' (pyirsdk) for live iRacing telemetry") from e
        self._irsdk = irsdk
        self.ir = irsdk.IRSDK()
        self.period = 1.0 / hz
        self._session_update = -1

    def wait_for_connection(self, poll_s: float = 2.0) -> None:
        announced = False
        while True:
            if self.ir.is_initialized and self.ir.is_connected:
                return
            self.ir.shutdown()
            if self.ir.startup() and self.ir.is_connected:
                return
            if not announced:
                log.info("Waiting for iRacing... (start a session and get in the car)")
                announced = True
            time.sleep(poll_s)

    @property
    def connected(self) -> bool:
        return bool(self.ir.is_initialized and self.ir.is_connected)

    def session_changed(self) -> bool:
        upd = self.ir["SessionInfoUpdate"] if self.connected else -1
        if upd != self._session_update:
            self._session_update = upd
            return True
        return False

    def session_info(self) -> SessionInfo:
        wi = self.ir["WeekendInfo"] or {}
        di = self.ir["DriverInfo"] or {}
        me = di.get("DriverCarIdx", 0)
        car = next((d for d in di.get("Drivers", []) if d.get("CarIdx") == me), {})
        stype = ""
        try:
            num = self.ir["SessionNum"]
            sessions = (self.ir["SessionInfo"] or {}).get("Sessions", [])
            stype = next((s.get("SessionType", "") for s in sessions if s.get("SessionNum") == num), "")
        except Exception:  # pragma: no cover - SDK quirks
            pass
        return SessionInfo(
            track_id=str(wi.get("TrackID", "unknown")),
            track_name=wi.get("TrackDisplayName", "Unknown track"),
            track_config=wi.get("TrackConfigName", "") or "",
            track_length_m=parse_track_length(wi.get("TrackLength")),
            car_id=car.get("CarPath", "unknown"),
            car_name=car.get("CarScreenName", "Unknown car"),
            session_type=stype,
        )

    def samples(self) -> Iterator[Sample]:
        """Yield samples until iRacing disconnects."""
        ir = self.ir
        last_tick = None
        while self.connected:
            ir.freeze_var_buffer_latest()
            tick = ir["SessionTick"]
            if tick == last_tick:
                ir.unfreeze_var_buffer_latest()
                time.sleep(self.period / 2)
                continue
            last_tick = tick
            try:
                yield Sample(
                    session_time=float(ir["SessionTime"]),
                    lap=int(ir["Lap"]),
                    lap_dist_pct=float(ir["LapDistPct"]),
                    speed=float(ir["Speed"]),
                    throttle=float(ir["Throttle"]),
                    brake=float(ir["Brake"]),
                    steer=float(ir["SteeringWheelAngle"]),
                    gear=int(ir["Gear"]),
                    rpm=float(ir["RPM"]),
                    on_pit_road=bool(ir["OnPitRoad"]),
                    track_surface=int(ir["PlayerTrackSurface"] if ir["PlayerTrackSurface"] is not None else TRK_ON_TRACK),
                    is_on_track=bool(ir["IsOnTrack"]),
                )
            except TypeError:
                # Variables are None while loading / in the garage.
                pass
            finally:
                ir.unfreeze_var_buffer_latest()
            time.sleep(self.period * 0.8)
