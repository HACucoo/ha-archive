"""One sync pass: catch every series up from its newest stored row to now."""

import asyncio
import fnmatch
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, sheets, wetter
from .geo import Places

log = logging.getLogger(__name__)

# (HA period, table, how much to request per call, how far each run goes back behind the last fetch)
# HA compiles an hour's statistics some minutes after it ended; the overlap catches late ones, the upsert the doubles
PERIODS = [
    ("hour", "stats_hour", timedelta(days=120), timedelta(hours=3)),
    ("5minute", "stats_5min", timedelta(days=2), timedelta(minutes=30)),
]
# Series per request: many for the hourly catch-up, few while a long backfill brings thousands of rows each
BATCH_SHORT = 200
BATCH_LONG = 10
# HA commits states a few seconds late; each state fetch goes back a little, the upsert drops the doubles
STATE_OVERLAP = timedelta(minutes=10)
# Open-Meteo's reanalysis trails by a few days: fetch once a day, the last ten days again each time
WEATHER_EVERY = timedelta(hours=20)
WEATHER_REFRESH_DAYS = 10


def in_thread(fn, *args):
    """Database work off the event loop, so the HA WebSocket keeps answering its pings meanwhile."""
    return asyncio.to_thread(fn, *args)


async def sync_once(ha, conn, cfg, now: datetime | None = None, resync: bool = False) -> int:
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    meta = await in_thread(db.upsert_meta, conn, await ha.statistic_ids())
    total = 0
    for period, table, chunk, overlap in PERIODS:
        total += await sync_statistics(ha, conn, cfg, meta, period, table, chunk, overlap, now, resync)
    total += await sync_positions(ha, conn, cfg, now, resync)
    if cfg.location_entities or cfg.state_entities or cfg.weather_history:
        states = await ha.states()
        set_home(conn, states)
        total += await sync_states(ha, conn, cfg, states, now, resync)
        if cfg.location_entities:
            update_places(conn)
        total += await sync_weather(conn, cfg, now)
    total += await sync_sheets(conn, cfg, now)
    seconds = time.monotonic() - started
    db.log_sync(conn, total, seconds)
    log.info("synced %d rows for %d series in %.1f s", total, len(meta), seconds)
    return total


async def sync_statistics(ha, conn, cfg, meta, period, table, chunk, overlap, now, resync) -> int:
    """Series that stopped at the same point are asked together. Each series remembers how far it was fetched
    (stat_marken), so one without new rows is not asked from HOURLY_SINCE again on every run."""
    # HA only keeps 5-minute rows for the recorder's purge window, nothing older to fetch
    floor = cfg.hourly_since if period == "hour" else now - timedelta(days=cfg.short_days)
    fetched = {} if resync else await in_thread(db.stat_marks, conn, table)
    # Before 0.7.0 there were no marks: the newest stored row says where to go on
    stored = {} if resync else await in_thread(db.watermarks, conn, table)
    groups: dict[datetime, list[tuple[str, int]]] = {}
    for statistic_id, meta_id in meta.items():
        if meta_id in fetched:
            cursor = fetched[meta_id] - overlap
        else:
            cursor = stored.get(meta_id, floor)
        groups.setdefault(max(cursor, floor), []).append((statistic_id, meta_id))
    total = 0
    for cursor, series in sorted(groups.items()):
        size = BATCH_SHORT if now - cursor <= chunk else BATCH_LONG
        for i in range(0, len(series), size):
            batch = dict(series[i:i + size])
            start = cursor
            while start < now:
                end = min(start + chunk, now)
                result = await ha.statistics(list(batch), start, end, period)
                for statistic_id, rows in result.items():
                    if statistic_id in batch:
                        total += await in_thread(db.insert_rows, conn, table, batch[statistic_id], rows)
                start = end
            await in_thread(db.set_stat_marks, conn, table, list(batch.values()), now)
            await in_thread(conn.commit)
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
            total += await in_thread(db.insert_positions, conn, entity_id, result.get(entity_id, []))
            cursor = end
        await in_thread(conn.commit)
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
    marks = await in_thread(db.state_marks, conn, wanted)
    groups: dict[datetime, list[str]] = {}
    for entity_id in wanted:
        mark = None if resync else marks.get(entity_id)
        groups.setdefault(max(mark or floor, floor), []).append(entity_id)
    total = 0
    for cursor, entity_ids in sorted(groups.items()):
        cursor = max(cursor - STATE_OVERLAP, floor)
        while cursor < now:
            end = min(cursor + timedelta(days=1), now)
            total += await in_thread(db.insert_states, conn, await ha.state_changes(entity_ids, cursor, end))
            cursor = end
        await in_thread(db.set_state_marks, conn, entity_ids, now)
        await in_thread(conn.commit)
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


async def sync_sheets(conn, cfg, now: datetime, download=None) -> int:
    """Each configured sheet: a new version is kept as a file and replaces its rows. The link is never logged."""
    total = 0
    for name, url in cfg.sheets:
        try:
            data = await asyncio.to_thread(download or sheets.download, url)
            tabs = sheets.read(data)
        except Exception as err:  # noqa: BLE001 — keep the last version, try again next run
            # Only the kind of error: the message of a malformed link would contain the link itself
            log.warning("sheet %s not fetched (%s%s), keeping the last version", name, type(err).__name__,
                        f" {err.code}" if isinstance(getattr(err, "code", None), int) else "")
            continue
        fingerprint = sheets.fingerprint(tabs)
        if fingerprint == db.sheet_fingerprint(conn, name):
            db.sheet_checked(conn, name, now)
            continue
        if cfg.sheets_dir:
            folder = Path(cfg.sheets_dir) / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{now.astimezone():%Y-%m-%d_%H%M}.xlsx").write_bytes(data)
        count = db.replace_sheet(conn, name, tabs, fingerprint, now)
        log.info("sheet %s changed: %d rows in %d tabs", name, count, len(tabs))
        total += count
    return total


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
