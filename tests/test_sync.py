from datetime import datetime, timedelta, timezone

import pytest

from archiv import db
from archiv.config import Config
from archiv.ha import HAClient
from archiv.sync import sync_once

NOW = datetime(2026, 10, 10, 20, tzinfo=timezone.utc)


def cfg(**kw):
    base = dict(ha_url="", ha_token="good", pg_dsn="", ro_password=None, interval_s=3600,
                hourly_since=datetime(2026, 9, 1, tzinfo=timezone.utc), short_days=10, provisioning_dir=None)
    base.update(kw)
    return Config(**base)


def hourly(start, count, value=1.0, total=None):
    return [{"start": (start + timedelta(hours=i)).timestamp() * 1000, "mean": value + i, "min": value,
             "max": value + i, "state": None, "sum": None if total is None else total + i}
            for i in range(count)]


def test_to_datetime():
    assert db.to_datetime(1791655200000) == datetime(2026, 10, 10, 18, tzinfo=timezone.utc)
    assert db.to_datetime("2026-10-10T18:00:00+00:00") == datetime(2026, 10, 10, 18, tzinfo=timezone.utc)


def test_setup_twice(pg):
    db.setup(pg, ro_password="ro")
    db.setup(pg, ro_password="ro2")
    assert pg.execute("SELECT count(*) FROM meta").fetchone()[0] == 0


def test_meta_and_upsert(pg):
    db.setup(pg)
    ids = db.upsert_meta(pg, [
        {"statistic_id": "sensor.t", "name": "T", "statistics_unit_of_measurement": "°C", "mean_type": 1,
         "has_sum": False, "source": "recorder"},
        {"statistic_id": "sensor.e", "has_mean": False, "has_sum": True, "source": "recorder"},
    ])
    again = db.upsert_meta(pg, [{"statistic_id": "sensor.t", "name": "T neu", "mean_type": 1}])
    assert again["sensor.t"] == ids["sensor.t"]
    assert pg.execute("SELECT name, has_mean FROM meta WHERE statistic_id = 'sensor.t'").fetchone() == ("T neu", True)

    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert db.insert_rows(pg, "stats_hour", ids["sensor.t"], hourly(start, 3)) == 3
    # Same hour again with a corrected value: updated, not duplicated
    db.insert_rows(pg, "stats_hour", ids["sensor.t"], [{"start": start.timestamp() * 1000, "mean": 42.0}])
    # Rows without any value are skipped
    assert db.insert_rows(pg, "stats_hour", ids["sensor.t"], [{"start": 0, "mean": None}]) == 0
    pg.commit()
    assert pg.execute("SELECT count(*), max(mean) FROM stats_hour").fetchone() == (3, 42.0)
    assert db.watermarks(pg, "stats_hour") == {ids["sensor.t"]: start + timedelta(hours=2)}
    assert pg.execute("SELECT statistic_id, unit FROM stunde LIMIT 1").fetchone() == ("sensor.t", "°C")


async def test_sync_catches_up(pg, fake_ha):
    db.setup(pg)
    fake_ha.statistic_ids = [
        {"statistic_id": "sensor.t", "mean_type": 1, "has_sum": False},
        {"statistic_id": "sensor.e", "mean_type": 0, "has_sum": True},
    ]
    since = datetime(2026, 9, 1, tzinfo=timezone.utc)
    fake_ha.rows[("sensor.t", "hour")] = hourly(since, 24 * 39)
    fake_ha.rows[("sensor.e", "hour")] = hourly(since, 24 * 39, total=100.0)
    fake_ha.rows[("sensor.t", "5minute")] = [
        {"start": (NOW - timedelta(minutes=5 * (i + 1))).timestamp() * 1000, "mean": 20.0} for i in range(12)
    ]

    async with HAClient(fake_ha.url, "good") as ha:
        first = await sync_once(ha, pg, cfg(), now=NOW)
    assert first == 24 * 39 * 2 + 12
    assert pg.execute("SELECT count(*) FROM stats_hour").fetchone()[0] == 24 * 39 * 2
    assert pg.execute("SELECT count(*) FROM sync_log").fetchone()[0] == 1

    # Next pass starts at each series' newest row, not at HOURLY_SINCE again
    fake_ha.requests.clear()
    later = NOW + timedelta(hours=1)
    fake_ha.rows[("sensor.t", "hour")] += hourly(since + timedelta(hours=24 * 39), 1)
    async with HAClient(fake_ha.url, "good") as ha:
        await sync_once(ha, pg, cfg(), now=later)
    hour_requests = [r for r in fake_ha.requests if r.get("period") == "hour"]
    assert all(r["start_time"] >= "2026-10-09" for r in hour_requests)
    assert pg.execute("SELECT count(*) FROM stats_hour").fetchone()[0] == 24 * 39 * 2 + 1

    # --resync goes back to the start
    fake_ha.requests.clear()
    async with HAClient(fake_ha.url, "good") as ha:
        await sync_once(ha, pg, cfg(), now=later, resync=True)
    assert min(r["start_time"] for r in fake_ha.requests if r.get("period") == "hour").startswith("2026-09-01")


def track(start, steps):
    """steps: (minutes after start, state, lat, lon)"""
    return [{"s": st, "lu": (start + timedelta(minutes=m)).timestamp(),
             "a": {"friendly_name": "Stephan", "latitude": lat, "longitude": lon, "gps_accuracy": 12,
                   "source": "device_tracker.nothing_phone_3"}}
            for m, st, lat, lon in steps]


async def test_positions_and_stays(pg, fake_ha):
    db.setup(pg)
    morning = NOW - timedelta(hours=12)
    fake_ha.history["person.stephan"] = track(morning, [
        (0, "home", 48.99, 12.07), (30, "home", 48.99, 12.07),
        (40, "not_home", 49.00, 12.07), (45, "not_home", 49.01, 12.07),
        (50, "Arbeit", 49.012, 12.074), (400, "Arbeit", 49.012, 12.074),
        (500, "home", 48.99, 12.07),
    ])
    # Older than HA keeps anyway: never asked for
    fake_ha.history["person.stephan"].insert(0, track(NOW - timedelta(days=20), [(0, "home", 1, 1)])[0])
    async with HAClient(fake_ha.url, "good") as ha:
        await sync_once(ha, pg, cfg(location_entities=("person.stephan",)), now=NOW)

    assert pg.execute("SELECT count(*), max(name), max(source) FROM positions").fetchone() == (
        7, "Stephan", "device_tracker.nothing_phone_3")
    history_requests = [r for r in fake_ha.requests if r["type"] == "history/history_during_period"]
    assert all(r["include_start_time_state"] is False for r in history_requests)
    assert min(r["start_time"] for r in history_requests) >= (NOW - timedelta(days=10)).isoformat()

    stays = pg.execute(
        "SELECT ort, extract(epoch FROM ende - beginn) / 60 FROM aufenthalte ORDER BY beginn"
    ).fetchall()
    assert stays == [("Zuhause", 40), ("unterwegs", 10), ("Arbeit", 450), ("Zuhause", None)]

    # Second run: nothing new, nothing duplicated
    async with HAClient(fake_ha.url, "good") as ha:
        await sync_once(ha, pg, cfg(location_entities=("person.stephan",)), now=NOW + timedelta(hours=1))
    assert pg.execute("SELECT count(*) FROM positions").fetchone()[0] == 7
