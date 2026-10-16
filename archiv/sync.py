"""One sync pass: catch every series up from its newest stored row to now."""

import asyncio
import fnmatch
import logging
import time
from datetime import datetime, timedelta, timezone

from . import db, wetter
from .geo import Places

log = logging.getLogger(__name__)

# (HA period, table, how much to request per call)
PERIODS = [
    ("hour", "stats_hour", timedelta(days=120)),
    ("5minute", "stats_5min", timedelta(days=2)),
]
# HA commits states a few seconds late; each state fetch goes back a little, the upsert drops the doubles
STATE_OVERLAP = timedelta(minutes=10)
# Open-Meteo's reanalysis trails by a few days: fetch once a day, the last ten days again each time
WEATHER_EVERY = timedelta(hours=20)
WEATHER_REFRESH_DAYS = 10


async def sync_once(ha, conn, cfg, now: datetime | None = None, resync: bool = False) -> int:
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    meta = db.upsert_meta(conn, await ha.statistic_ids())
    total = 0
    for period, table, chunk in PERIODS:
        # HA only keeps 5-minute rows for the recorder's purge window, nothing older to fetch
        floor = cfg.hourly_since if period == "hour" else now - timedelta(days=cfg.short_days)
        marks = {} if resync else db.watermarks(conn, table)
        for statistic_id, meta_id in meta.items():
            # The newest stored row is fetched again; an upsert keeps it current
            cursor = max(marks.get(meta_id, floor), floor)
            while cursor < now:
                end = min(cursor + chunk, now)
                result = await ha.statistics([statistic_id], cursor, end, period)
                total += db.insert_rows(conn, table, meta_id, result.get(statistic_id, []))
                cursor = end
            conn.commit()
    total += await sync_positions(ha, conn, cfg, now, resync)
    if cfg.location_entities or cfg.state_entities or cfg.weather_history:
        states = await ha.states()
        set_home(conn, states)
        total += await sync_states(ha, conn, cfg, states, now, resync)
        if cfg.location_entities:
            update_places(conn)
        total += await sync_weather(conn, cfg, now)
    seconds = time.monotonic() - started
    db.log_sync(conn, total, seconds)
    log.info("synced %d rows for %d series in %.1f s", total, len(meta), seconds)
    return total


async def sync_positions(ha, conn, cfg, now: datetime, resync: bool) -> int:
    """Positions and zones of the configured people; HA keeps them for the recorder's purge window only."""
    floor = now - timedelta(days=cfg.short_days)
    marks = {} if resync else db.position_marks(conn)
    total = 0
    for entity_id in cfg.location_entities:
        cursor = max(marks.get(entity_id, floor), floor)
        while cursor < now:
            end = min(cursor + timedelta(days=1), now)
            result = await ha.history([entity_id], cursor, end)
            total += db.insert_positions(conn, entity_id, result.get(entity_id, []))
            cursor = end
        conn.commit()
    return total


def wanted_entities(cfg, states: list[dict]) -> dict[str, str | None]:
    """entity_id -> friendly name of every entity matching STATE_ENTITIES and not STATE_EXCLUDE."""
    def matches(entity_id, patterns):
        return any(fnmatch.fnmatchcase(entity_id, p) for p in patterns)

    return {
        s["entity_id"]: s.get("attributes", {}).get("friendly_name")
        for s in states
        if matches(s["entity_id"], cfg.state_entities) and not matches(s["entity_id"], cfg.state_exclude)
    }


async def sync_states(ha, conn, cfg, states: list[dict], now: datetime, resync: bool) -> int:
    """State changes of the configured entities, from where each one stopped (a new one from HA's purge window)."""
    if not cfg.state_entities:
        return 0
    wanted = wanted_entities(cfg, states)
    if not wanted:
        return 0
    floor = now - timedelta(days=cfg.short_days)
    marks = db.state_marks(conn, wanted)
    groups: dict[datetime, list[str]] = {}
    for entity_id in wanted:
        mark = None if resync else marks.get(entity_id)
        groups.setdefault(max(mark or floor, floor), []).append(entity_id)
    total = 0
    for cursor, entity_ids in sorted(groups.items()):
        cursor = max(cursor - STATE_OVERLAP, floor)
        while cursor < now:
            end = min(cursor + timedelta(days=1), now)
            total += db.insert_states(conn, await ha.state_changes(entity_ids, cursor, end))
            cursor = end
        db.set_state_marks(conn, entity_ids, now)
        conn.commit()
    return total


async def sync_weather(conn, cfg, now: datetime, fetch=None) -> int:
    """Daily temperatures at home since HOURLY_SINCE; a failed download waits for the next run."""
    if not cfg.weather_history:
        return 0
    last = db.get_mark(conn, "wetter")
    home = db.home(conn)
    if home is None or (last and now - last < WEATHER_EVERY):
        return 0
    newest = db.newest_weather_day(conn)
    start = newest - timedelta(days=WEATHER_REFRESH_DAYS) if newest else cfg.hourly_since.date()
    end = (now - timedelta(days=1)).date()
    try:
        rows = await asyncio.to_thread(fetch or wetter.fetch, home[0], home[1], start, end)
    except Exception as err:  # noqa: BLE001 — no internet must not stop the archive
        log.warning("Open-Meteo not reachable, trying again next run: %s", err)
        return 0
    count = db.upsert_weather(conn, rows)
    db.set_mark(conn, "wetter", now)
    conn.commit()
    return count


def set_home(conn, states: list[dict]) -> None:
    home = next((s for s in states if s["entity_id"] == "zone.home"), None)
    if home:
        db.set_home(conn, home["attributes"]["latitude"], home["attributes"]["longitude"])


_places: Places | None = None


def update_places(conn) -> None:
    """Town names for the "heute vor" view."""
    global _places
    if not Places.available():
        return
    if _places is None:
        _places = Places()
    named = db.fill_place_names(conn, _places)
    if named:
        log.info("named %d new place squares", named)
