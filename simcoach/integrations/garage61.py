"""Garage 61 (https://garage61.net) - import reference laps.

Garage 61 stores your laps and your teammates' laps with telemetry. Using a
faster teammate's lap (or your own all-time best) as the reference lets the
coach compare you against a better target than today's best.

Create a personal access token at https://garage61.net/developer with the
``driving_data`` scope and set ``GARAGE61_TOKEN`` (or ``garage61.token`` in the
config). Telemetry export requires a Garage 61 Pro plan. The API only returns
laps by you and your teammates/followed drivers - there's no global search.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from simcoach.analysis.lap import Lap
from simcoach.telemetry.files import lap_from_csv_text

BASE_URL = "https://garage61.net/api/v1/"


class Garage61Error(RuntimeError):
    pass


class Garage61:
    def __init__(self, token: str, base_url: str = BASE_URL, timeout: float = 30.0):
        if not token:
            raise Garage61Error("No Garage 61 token: set GARAGE61_TOKEN or garage61.token in the config")
        self.token = token
        self.base_url = base_url
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> bytes:
        url = self.base_url + path.lstrip("/")
        if params:
            clean = {k: (",".join(map(str, v)) if isinstance(v, (list, tuple)) else v)
                     for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(clean)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}",
                                                   "Accept": "application/json, text/csv"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise Garage61Error(f"Garage 61 {e.code} on {path}: {body}") from e

    def _json(self, path: str, params: dict | None = None):
        return json.loads(self._get(path, params))

    def me(self) -> dict:
        return self._json("me")

    def tracks(self) -> list[dict]:
        return self._json("tracks").get("items", [])

    def cars(self) -> list[dict]:
        return self._json("cars").get("items", [])

    def find_laps(self, track: int | None = None, car: int | None = None, drivers: str | None = "me",
                  group: str = "driver", limit: int = 20, telemetry: bool = True) -> list[dict]:
        """Best laps per driver for a track/car. ``drivers``: "me", "following" or None (you + teammates)."""
        params = {
            "tracks": track, "cars": car, "drivers": drivers, "group": group, "limit": limit,
            "seeTelemetry": "true" if telemetry else None,
        }
        return self._json("laps", params).get("items", [])

    def lap(self, lap_id: str) -> dict:
        return self._json(f"laps/{lap_id}")

    def lap_csv(self, lap_id: str) -> str:
        return self._get(f"laps/{lap_id}/csv").decode("utf-8-sig")

    def lap_telemetry(self, lap_id: str, track_length_m: float | None = None) -> Lap:
        lap = lap_from_csv_text(self.lap_csv(lap_id), track_length_m, source=f"garage61:{lap_id}")
        try:
            info = self.lap(lap_id)
            lt = info.get("lapTime")
            if isinstance(lt, (int, float)) and lt > 0:
                lap.lap_time = float(lt)
            lap.meta["garage61"] = {k: info.get(k) for k in ("id", "lapTime", "driver", "car", "track") if k in info}
        except (Garage61Error, ValueError):
            pass
        return lap


def describe_lap(item: dict) -> str:
    """One-line summary of a lap search result (field names are best-effort)."""
    def name(x):
        if isinstance(x, dict):
            return x.get("name") or x.get("displayName") or x.get("slug") or x.get("id")
        return x

    lt = item.get("lapTime")
    t = f"{int(lt // 60)}:{lt % 60:06.3f}" if isinstance(lt, (int, float)) else "?"
    return f"{item.get('id')}  {t}  {name(item.get('driver'))}  {name(item.get('car'))}  {name(item.get('track'))}"
