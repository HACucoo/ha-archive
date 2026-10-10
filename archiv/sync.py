"""One sync pass: catch every series up from its newest stored row to now."""

import logging
import time
from datetime import datetime, timedelta, timezone

from . import db

log = logging.getLogger(__name__)

# (HA period, table, how much to request per call)
PERIODS = [
    ("hour", "stats_hour", timedelta(days=120)),
    ("5minute", "stats_5min", timedelta(days=2)),
]


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
    seconds = time.monotonic() - started
    db.log_sync(conn, total, seconds)
    log.info("synced %d rows for %d series in %.1f s", total, len(meta), seconds)
    return total
