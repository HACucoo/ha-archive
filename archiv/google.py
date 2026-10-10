"""Google Maps timeline export (on-device "Zeitachse" JSON) -> rows for the positions table.

State per row, so that the aufenthalte view reads Google's history like HA's:
- a visit labelled HOME is 'home', WORK is 'Arbeit', whatever the address was back then. Google labels
  each visit on its own and often leaves one at home or work unlabelled, so a place labelled HOME (WORK)
  at least LABEL_MIN times counts for every visit between its first and last such label
- any other visit takes the name of the HA zone it lies in, else 'anderer Ort'
- path points take the state of the visit they fall into, else 'not_home'
- the start of every activity (a trip) is 'not_home', it ends the stay before
"""

import bisect
import json
import math
import re
from datetime import datetime

HOME, AWAY, OTHER, WORK = "home", "not_home", "anderer Ort", "Arbeit"
LABEL_MIN = 3
LABELS = {"HOME": HOME, "INFERRED_HOME": HOME, "WORK": WORK, "INFERRED_WORK": WORK}
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def parse_latlng(value: str) -> tuple[float, float] | None:
    """'48.99°, 12.07°' (Android) or 'geo:48.99,12.07' (iOS)."""
    numbers = _NUMBER.findall(value or "")
    if len(numbers) < 2:
        return None
    return float(numbers[0]), float(numbers[1])


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def zones_from_states(states: list[dict]) -> list[tuple[str, float, float, float]]:
    """(state name, lat, lon, radius) for HA's zones, smallest first; zone.home is 'home' as in person states."""
    zones = []
    for s in states:
        if not s["entity_id"].startswith("zone.") or s["attributes"].get("passive"):
            continue
        a = s["attributes"]
        name = HOME if s["entity_id"] == "zone.home" else a.get("friendly_name", s["entity_id"])
        zones.append((name, a["latitude"], a["longitude"], a.get("radius", 100)))
    return sorted(zones, key=lambda z: z[3])


def zone_at(point, zones) -> str | None:
    for name, lat, lon, radius in zones:
        if distance_m(point, (lat, lon)) <= radius:
            return name
    return None


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def segments(data) -> list[dict]:
    if isinstance(data, dict):
        return data.get("semanticSegments", [])
    raise ValueError("unknown export format: expected the on-device timeline export with semanticSegments")


def rows(data, zones) -> list[tuple[datetime, str, float, float]]:
    """Sorted (time, state, lat, lon); one row per time, later rules win."""
    segs = segments(data)
    out: dict[datetime, tuple[str, float, float]] = {}

    top_visits = [s for s in segs if s.get("visit") and s["visit"].get("hierarchyLevel", 0) == 0]
    windows = _label_windows(top_visits)

    visits = []  # (start, end, state)
    for s in top_visits:
        top = s["visit"].get("topCandidate", {})
        point = parse_latlng(top.get("placeLocation", {}).get("latLng", ""))
        if not point:
            continue
        start, end = _time(s["startTime"]), _time(s["endTime"])
        state = LABELS.get(top.get("semanticType")) or next(
            (label for label, first, last in windows.get(top.get("placeId"), []) if first <= start <= last),
            None,
        ) or zone_at(point, zones) or OTHER
        visits.append((start, end, state))
        out[start] = (state, *point)
        out[end] = (state, *point)
    visits.sort()
    starts = [v[0] for v in visits]

    for s in segs:
        for p in s.get("timelinePath", []):
            point = parse_latlng(p.get("point", ""))
            if not point:
                continue
            t = _time(p["time"])
            i = bisect.bisect_right(starts, t) - 1
            state = visits[i][2] if i >= 0 and visits[i][0] <= t <= visits[i][1] else AWAY
            out.setdefault(t, (state, *point))

    for s in segs:
        activity = s.get("activity")
        if not activity:
            continue
        point = parse_latlng(activity.get("start", {}).get("latLng", ""))
        if point:
            out[_time(s["startTime"])] = (AWAY, *point)

    return [(t, *v) for t, v in sorted(out.items())]


def _label_windows(visits) -> dict[str, list[tuple[str, datetime, datetime]]]:
    """placeId -> (state, first, last) for HOME/WORK labels seen at least LABEL_MIN times."""
    seen: dict[tuple[str, str], list[datetime]] = {}
    for s in visits:
        top = s["visit"].get("topCandidate", {})
        label = LABELS.get(top.get("semanticType"))
        if label and top.get("placeId"):
            seen.setdefault((top["placeId"], label), []).append(_time(s["startTime"]))
    windows: dict[str, list] = {}
    for (place, label), times in seen.items():
        if len(times) >= LABEL_MIN:
            windows.setdefault(place, []).append((label, min(times), max(times)))
    return windows


def load(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
