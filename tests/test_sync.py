from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from archiv import db, wetter
from archiv.config import Config
from archiv.ha import HAClient
from archiv.sync import sync_once, sync_sheets, sync_weather, wanted_entities

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
        # Never has a row: must not be asked from HOURLY_SINCE on every run
        {"statistic_id": "sensor.leer", "mean_type": 1, "has_sum": False},
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

    # Next pass: all series together, from a little before where the last one stopped, not from HOURLY_SINCE
    fake_ha.requests.clear()
    later = NOW + timedelta(hours=1)
    fake_ha.rows[("sensor.t", "hour")] += hourly(since + timedelta(hours=24 * 39), 1)
    async with HAClient(fake_ha.url, "good") as ha:
        await sync_once(ha, pg, cfg(), now=later)
    hour_requests = [r for r in fake_ha.requests if r.get("period") == "hour"]
    assert len(hour_requests) == 1
    assert set(hour_requests[0]["statistic_ids"]) == {"sensor.t", "sensor.e", "sensor.leer"}
    assert hour_requests[0]["start_time"] == (NOW - timedelta(hours=3)).isoformat()
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


def changes(start, steps):
    """steps: (minutes after start, state); HA's minimal format, the first row a full one with lc"""
    rows = [{"s": st, "lu": (start + timedelta(minutes=m)).timestamp()} for m, st in steps]
    if rows:
        rows[0] = {"s": rows[0]["s"], "a": {}, "lu": rows[0]["lu"] + 1, "lc": rows[0]["lu"]}
    return rows


def entity(entity_id, name):
    return {"entity_id": entity_id, "state": "off", "attributes": {"friendly_name": name}}


def test_wanted_entities():
    states = [entity("binary_sensor.door", "Tür"), entity("binary_sensor.fw_update", "Update"),
              entity("light.lamp", "Lampe"), entity("sensor.t", "T")]
    wanted = wanted_entities(cfg(state_entities=("binary_sensor.*", "light.*"),
                                 state_exclude=("binary_sensor.*_update",)), states)
    assert wanted == {"binary_sensor.door": "Tür", "light.lamp": "Lampe"}


async def test_state_changes(pg, fake_ha):
    db.setup(pg)
    settings = cfg(state_entities=("binary_sensor.*",))
    fake_ha.states += [entity("binary_sensor.door", "Haustür"), entity("light.lamp", "Lampe")]
    start = NOW - timedelta(hours=5)
    fake_ha.history["binary_sensor.door"] = changes(start, [(0, "off"), (60, "on"), (75, "off")])
    fake_ha.history["light.lamp"] = changes(start, [(0, "on")])
    # Older than HA keeps anyway: never asked for
    fake_ha.history["binary_sensor.door"].insert(0, {"s": "on", "lu": (NOW - timedelta(days=20)).timestamp()})

    async with HAClient(fake_ha.url, "good") as ha:
        await sync_once(ha, pg, settings, now=NOW)
    assert pg.execute("SELECT count(*), count(DISTINCT entity_id) FROM zustaende").fetchone() == (3, 1)
    # The full first row counts from its last_changed, not from an attribute update
    assert pg.execute('SELECT min("time") FROM zustaende').fetchone()[0] == start
    requests = [r for r in fake_ha.requests if r["type"] == "history/history_during_period"]
    assert all(r["minimal_response"] and r["no_attributes"] and r["include_start_time_state"] is False
               for r in requests)
    assert min(r["start_time"] for r in requests) >= (NOW - timedelta(days=10)).isoformat()
    phases = pg.execute(
        "SELECT name, state, extract(epoch FROM ende - beginn) / 60 FROM zustand_phasen ORDER BY beginn"
    ).fetchall()
    assert phases == [("Haustür", "off", 60), ("Haustür", "on", 15), ("Haustür", "off", None)]

    # Next run: only from where it stopped (minus the overlap), no doubles; a newly matching entity
    # is fetched from the start of HA's window
    fake_ha.requests.clear()
    later = NOW + timedelta(hours=1)
    fake_ha.history["binary_sensor.door"] += changes(NOW + timedelta(minutes=30), [(0, "on")])
    fake_ha.states.append(entity("binary_sensor.window", "Fenster"))
    fake_ha.history["binary_sensor.window"] = changes(NOW - timedelta(days=3), [(0, "on"), (10, "off")])
    async with HAClient(fake_ha.url, "good") as ha:
        await sync_once(ha, pg, settings, now=later)
    assert pg.execute("SELECT count(*) FROM zustaende").fetchone()[0] == 6
    requests = [r for r in fake_ha.requests if r["type"] == "history/history_during_period"]
    door = [r for r in requests if "binary_sensor.door" in r["entity_ids"]]
    assert door and min(r["start_time"] for r in door) == (NOW - timedelta(minutes=10)).isoformat()
    assert "binary_sensor.window" not in door[0]["entity_ids"]
    assert pg.execute("SELECT count(*) FROM zustand_entitaeten WHERE bis = %s", (later,)).fetchone()[0] == 2


async def test_weather(pg, fake_ha, monkeypatch):
    db.setup(pg)
    settings = cfg(weather_history=True, hourly_since=datetime(2026, 9, 1, tzinfo=timezone.utc))
    calls = []

    def fetch(lat, lon, start, end):
        calls.append((lat, lon, start, end))
        days = (end - start).days + 1
        # The last two days are not in the reanalysis yet
        return [(start + timedelta(days=i), None if i >= days - 2 else 10.0 + i % 8, 5.0, 15.0) for i in range(days)]

    monkeypatch.setattr(wetter, "fetch", fetch)
    async with HAClient(fake_ha.url, "good") as ha:
        await sync_once(ha, pg, settings, now=NOW)
    assert db.home(pg) == (48.9947, 12.0762)
    assert calls == [(48.9947, 12.0762, datetime(2026, 9, 1).date(), datetime(2026, 10, 9).date())]
    assert pg.execute("SELECT count(*), max(tag) FROM wetter_tag").fetchone() == (37, datetime(2026, 10, 7).date())

    # Within 20 hours: nothing; a day later the last ten days again
    assert await sync_weather(pg, settings, NOW + timedelta(hours=5)) == 0
    await sync_weather(pg, settings, NOW + timedelta(days=1))
    assert calls[-1][2] == datetime(2026, 9, 27).date()
    row = pg.execute("SELECT gradtagzahl, heizgradtage FROM gradtage WHERE tag = '2026-09-01'").fetchone()
    assert row == (10.0, 5.0)
    assert pg.execute("SELECT max(gradtagzahl) FROM gradtage WHERE temp_mittel >= 15").fetchone()[0] == 0

    # No internet: logged, tried again next run
    def broken(*_):
        raise OSError("offline")

    assert await sync_weather(pg, settings, NOW + timedelta(days=3), broken) == 0
    assert db.get_mark(pg, "wetter") == NOW + timedelta(days=1)


def test_open_meteo_rows():
    data = {"daily": {"time": ["2026-10-01", "2026-10-02"], "temperature_2m_mean": [9.4, None],
                      "temperature_2m_min": [4.1, None], "temperature_2m_max": [14.0, None]}}
    assert wetter.rows(data) == [(datetime(2026, 10, 1).date(), 9.4, 4.1, 14.0),
                                 (datetime(2026, 10, 2).date(), None, None, None)]


async def test_sheets(pg, tmp_path):
    from test_sheets import workbook

    db.setup(pg, ro_password="ro", private_password="pv")
    settings = cfg(sheets=(("eins", "https://docs.google.com/spreadsheets/d/x/edit"),), sheets_dir=str(tmp_path))
    version = {"data": workbook({"T": [["Datum", "Wert"], [datetime(2026, 1, 1), 1], [datetime(2026, 1, 2), 2]]})}

    def download(url):
        return version["data"]

    assert await sync_sheets(pg, settings, NOW, download) == 2
    assert len(list((tmp_path / "eins").iterdir())) == 1
    rows = pg.execute("SELECT blatt, zeile, daten->>'Datum', (daten->>'Wert')::int FROM privat.sheet_zeilen"
                      " ORDER BY zeile").fetchall()
    assert rows == [("T", 2, "2026-01-01", 1), ("T", 3, "2026-01-02", 2)]

    # Unchanged: no new file, rows untouched, only the check time moves on
    assert await sync_sheets(pg, settings, NOW + timedelta(hours=1), download) == 0
    assert len(list((tmp_path / "eins").iterdir())) == 1
    assert pg.execute("SELECT geaendert, abgerufen FROM privat.sheet_quellen").fetchone() == (
        NOW, NOW + timedelta(hours=1))

    # Changed: a new version, rows replaced
    version["data"] = workbook({"T": [["Datum", "Wert"], [datetime(2026, 1, 1), 5]]})
    assert await sync_sheets(pg, settings, NOW + timedelta(hours=2), download) == 1
    assert len(list((tmp_path / "eins").iterdir())) == 2
    assert pg.execute("SELECT count(*) FROM privat.sheet_zeilen").fetchone()[0] == 1

    # Not reachable: the last version stays
    def broken(url):
        raise OSError("offline")

    assert await sync_sheets(pg, settings, NOW + timedelta(hours=3), broken) == 0
    assert pg.execute("SELECT count(*) FROM privat.sheet_zeilen").fetchone()[0] == 1


def test_private_schema_is_private(pg):
    db.setup(pg, ro_password="ro", private_password="pv")
    pg.execute("SET ROLE grafana_ro")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg.execute("SELECT * FROM privat.sheet_zeilen")
    pg.rollback()
    pg.execute("SET ROLE grafana_privat")
    pg.execute("SELECT * FROM privat.sheet_zeilen")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg.execute("SELECT * FROM meta")
    pg.rollback()


async def test_repeated_states_are_no_change(pg, fake_ha):
    db.setup(pg)
    settings = cfg(state_entities=("binary_sensor.*",))
    fake_ha.states.append(entity("binary_sensor.door", "Haustür"))
    start = NOW - timedelta(hours=5)
    # 60: HA restarted and stamped the unchanged state again
    fake_ha.history["binary_sensor.door"] = changes(start, [(0, "off"), (30, "on"), (60, "on"), (90, "off")])
    async with HAClient(fake_ha.url, "good") as ha:
        await sync_once(ha, pg, settings, now=NOW)
    assert pg.execute('SELECT state FROM zustaende ORDER BY "time"').fetchall() == [("off",), ("on",), ("off",)]

    # The next fetch starts with the state already stored: not stored again
    fake_ha.history["binary_sensor.door"] += changes(NOW + timedelta(minutes=20), [(0, "off"), (10, "on")])
    async with HAClient(fake_ha.url, "good") as ha:
        await sync_once(ha, pg, settings, now=NOW + timedelta(hours=1))
    assert [r[0] for r in pg.execute('SELECT state FROM zustaende ORDER BY "time"').fetchall()] == [
        "off", "on", "off", "on"]
    phases = pg.execute("SELECT extract(epoch FROM ende - beginn) / 60 FROM zustand_phasen ORDER BY beginn").fetchall()
    assert phases[1] == (60,)

    # Rows archived before 0.7.0 are cleaned once at setup
    pg.execute("INSERT INTO zustaende VALUES ('binary_sensor.door', %s, 'on')", (NOW + timedelta(hours=2),))
    pg.execute("DELETE FROM sync_marken WHERE name = 'zustaende_ohne_wiederholung'")
    pg.commit()
    db.setup(pg)
    assert pg.execute("SELECT count(*) FROM zustaende").fetchone()[0] == 4


def test_view_with_other_columns_is_recreated(pg):
    db.setup(pg, ro_password="ro")
    # As if an older version had made the view with other columns
    pg.execute("DROP VIEW gradtage")
    pg.execute("CREATE VIEW gradtage AS SELECT tag, temp_mittel, 1 AS alt, 2 AS noch_aelter FROM wetter_tag")
    pg.commit()
    db.setup(pg, ro_password="ro")
    columns = [r[0] for r in pg.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'gradtage' ORDER BY ordinal_position")]
    assert columns == ["tag", "temp_mittel", "gradtagzahl", "heizgradtage"]
    pg.execute("SET ROLE grafana_ro")
    pg.execute("SELECT * FROM gradtage")
    pg.rollback()


async def test_sheet_errors_do_not_log_the_link(pg, caplog):
    db.setup(pg)
    link = "https://docs.google.com/spreadsheets/d/geheim/edit"
    settings = cfg(sheets=(("eins", link),))

    def broken(url):
        raise ValueError(f"unknown url type: {url!r}")

    assert await sync_sheets(pg, settings, NOW, broken) == 0
    assert "eins" in caplog.text and "ValueError" in caplog.text
    assert "geheim" not in caplog.text


async def test_failed_login_closes_the_socket(fake_ha):
    from archiv.ha import HAError

    client = HAClient(fake_ha.url, "bad")
    with pytest.raises(HAError):
        async with client:
            pass
    assert client.ws.close_code is not None
