"""PostgreSQL side: schema, metadata, upserts. TimescaleDB is used when available."""

import logging
from datetime import datetime, timezone

import psycopg
from psycopg import sql

log = logging.getLogger(__name__)

TABLES = {
    # table: (time column, compression segment column, chunk interval, compress chunks older than)
    "stats_hour": ("start", "meta_id", "90 days", "90 days"),
    "stats_5min": ("start", "meta_id", "7 days", "14 days"),
    "positions": ("time", "entity_id", "30 days", "30 days"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id           serial PRIMARY KEY,
    statistic_id text UNIQUE NOT NULL,
    name         text,
    unit         text,
    has_mean     boolean NOT NULL DEFAULT false,
    has_sum      boolean NOT NULL DEFAULT false,
    source       text,
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS stats_hour (
    meta_id integer NOT NULL REFERENCES meta(id),
    start   timestamptz NOT NULL,
    mean double precision, min double precision, max double precision,
    state double precision, sum double precision,
    PRIMARY KEY (meta_id, start)
);
CREATE TABLE IF NOT EXISTS stats_5min (LIKE stats_hour INCLUDING ALL);
CREATE TABLE IF NOT EXISTS sync_log (
    finished timestamptz PRIMARY KEY,
    rows     integer NOT NULL,
    seconds  real NOT NULL
);
CREATE OR REPLACE VIEW stunde AS
    SELECT m.statistic_id, m.name, m.unit, s.start AS "time", s.mean, s.min, s.max, s.state, s.sum
    FROM stats_hour s JOIN meta m ON m.id = s.meta_id;
CREATE OR REPLACE VIEW fuenf_minuten AS
    SELECT m.statistic_id, m.name, m.unit, s.start AS "time", s.mean, s.min, s.max, s.state, s.sum
    FROM stats_5min s JOIN meta m ON m.id = s.meta_id;
CREATE TABLE IF NOT EXISTS positions (
    entity_id text NOT NULL,
    "time"    timestamptz NOT NULL,
    name      text,
    state     text,
    latitude  double precision,
    longitude double precision,
    accuracy  double precision,
    source    text,
    PRIMARY KEY (entity_id, "time")
);
-- One row per stay: consecutive updates in the same zone (or on the road) form one stay
CREATE OR REPLACE VIEW aufenthalte AS
    WITH changes AS (
        SELECT entity_id, name, "time", state,
            CASE WHEN state IS DISTINCT FROM lag(state) OVER w THEN 1 ELSE 0 END AS neu
        FROM positions WINDOW w AS (PARTITION BY entity_id ORDER BY "time")
    ), stays AS (
        SELECT *, sum(neu) OVER (PARTITION BY entity_id ORDER BY "time") AS nr FROM changes
    )
    SELECT entity_id, max(name) AS name,
        CASE state WHEN 'home' THEN 'Zuhause' WHEN 'not_home' THEN 'unterwegs' ELSE state END AS ort,
        min("time") AS beginn,
        lead(min("time")) OVER (PARTITION BY entity_id ORDER BY min("time")) AS ende
    FROM stays GROUP BY entity_id, state, nr;
CREATE TABLE IF NOT EXISTS heim (
    id        integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    latitude  double precision NOT NULL,
    longitude double precision NOT NULL
);
-- Nearest town per ~1 km square of the positions, from GeoNames; ort is NULL far from any town
CREATE TABLE IF NOT EXISTS orte_namen (
    lat2 numeric(5, 2) NOT NULL,
    lon2 numeric(5, 2) NOT NULL,
    ort  text,
    land text,
    km   real,
    PRIMARY KEY (lat2, lon2)
);
-- "Heute vor X Jahren": per person and earlier year the place furthest from home on this calendar day,
-- for today and tomorrow (so it is at hand for a morning briefing while the archive host sleeps)
CREATE OR REPLACE VIEW heute_vor AS
    WITH tage AS (
        SELECT ((now() AT TIME ZONE 'Europe/Berlin')::date + n) AS tag FROM generate_series(0, 1) n
    ), punkte AS (
        SELECT t.tag, p.entity_id, p.name, p.latitude, p.longitude,
            extract(year FROM t.tag)::int - extract(year FROM p."time" AT TIME ZONE 'Europe/Berlin')::int AS jahre,
            2 * 6371 * asin(sqrt(power(sin(radians(p.latitude - h.latitude) / 2), 2)
                + cos(radians(h.latitude)) * cos(radians(p.latitude))
                * power(sin(radians(p.longitude - h.longitude) / 2), 2))) AS km
        FROM tage t
        JOIN positions p
            ON to_char(p."time" AT TIME ZONE 'Europe/Berlin', 'MM-DD') = to_char(t.tag, 'MM-DD')
            AND (p."time" AT TIME ZONE 'Europe/Berlin')::date < t.tag
        CROSS JOIN heim h
        WHERE p.latitude IS NOT NULL
    ), weitester AS (
        SELECT DISTINCT ON (tag, entity_id, jahre) * FROM punkte
        WHERE jahre > 0
        ORDER BY tag, entity_id, jahre, km DESC
    )
    SELECT to_char(w.tag, 'MM-DD') AS tag, w.entity_id, w.name, w.jahre, o.ort, o.land, round(w.km)::integer AS km
    FROM weitester w
    JOIN orte_namen o ON o.lat2 = round(w.latitude::numeric, 2) AND o.lon2 = round(w.longitude::numeric, 2)
    WHERE w.km >= 50 AND o.ort IS NOT NULL;
"""


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn)


def setup(conn: psycopg.Connection, ro_password: str | None = None) -> bool:
    """Create tables and views; returns True when TimescaleDB is in use."""
    timescale = _try(conn, "CREATE EXTENSION IF NOT EXISTS timescaledb")
    conn.execute(SCHEMA)
    conn.commit()
    if timescale:
        for table, (time_col, segment_col, chunk, compress_after) in TABLES.items():
            conn.execute(
                "SELECT create_hypertable(%s, by_range(%s, %s::interval),"
                " if_not_exists => TRUE, migrate_data => TRUE)",
                (table, time_col, chunk),
            )
            enabled = conn.execute(
                "SELECT compression_enabled FROM timescaledb_information.hypertables"
                " WHERE hypertable_name = %s",
                (table,),
            ).fetchone()
            if enabled and not enabled[0]:
                conn.execute(sql.SQL(
                    "ALTER TABLE {} SET (timescaledb.compress,"
                    " timescaledb.compress_segmentby = {}, timescaledb.compress_orderby = {})"
                ).format(sql.Identifier(table), sql.Literal(segment_col), sql.Literal(time_col)))
            conn.execute(
                "SELECT add_compression_policy(%s, %s::interval, if_not_exists => TRUE)",
                (table, compress_after),
            )
        conn.commit()
    if ro_password:
        _readonly_role(conn, ro_password)
    return timescale


def _try(conn, statement) -> bool:
    try:
        conn.execute(statement)
        conn.commit()
        return True
    except psycopg.Error as err:
        conn.rollback()
        log.info("TimescaleDB not available (%s), using plain tables", err.diag.message_primary)
        return False


def _readonly_role(conn, password: str) -> None:
    """Login role for Grafana that can only read."""
    conn.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro')"
        " THEN CREATE ROLE grafana_ro LOGIN; END IF; END $$"
    )
    conn.execute(sql.SQL("ALTER ROLE grafana_ro WITH LOGIN PASSWORD {}").format(sql.Literal(password)))
    conn.execute("GRANT USAGE ON SCHEMA public TO grafana_ro")
    conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro")
    conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro")
    conn.commit()


def upsert_meta(conn: psycopg.Connection, items: list[dict]) -> dict[str, int]:
    """Store HA's statistic metadata, return statistic_id -> meta.id."""
    ids = {}
    for item in items:
        # HA 2025.4+ reports mean_type (0 = none), older versions has_mean
        has_mean = bool(item.get("mean_type", item.get("has_mean", False)))
        row = conn.execute(
            """
            INSERT INTO meta (statistic_id, name, unit, has_mean, has_sum, source)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (statistic_id) DO UPDATE SET
                name = COALESCE(EXCLUDED.name, meta.name), unit = COALESCE(EXCLUDED.unit, meta.unit),
                has_mean = EXCLUDED.has_mean,
                has_sum = EXCLUDED.has_sum, source = EXCLUDED.source, last_seen = now()
            RETURNING id
            """,
            (
                item["statistic_id"],
                item.get("name"),
                item.get("statistics_unit_of_measurement") or item.get("display_unit_of_measurement"),
                has_mean,
                bool(item.get("has_sum")),
                item.get("source"),
            ),
        ).fetchone()
        ids[item["statistic_id"]] = row[0]
    conn.commit()
    return ids


def watermarks(conn: psycopg.Connection, table: str) -> dict[int, datetime]:
    """Newest stored row per series."""
    query = sql.SQL("SELECT meta_id, max(start) FROM {} GROUP BY meta_id").format(sql.Identifier(table))
    return dict(conn.execute(query).fetchall())


def to_datetime(value) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    return datetime.fromisoformat(value)


def insert_rows(conn: psycopg.Connection, table: str, meta_id: int, rows: list[dict]) -> int:
    values = [
        (meta_id, to_datetime(r["start"]), r.get("mean"), r.get("min"), r.get("max"), r.get("state"), r.get("sum"))
        for r in rows
        if any(r.get(k) is not None for k in ("mean", "min", "max", "state", "sum"))
    ]
    if not values:
        return 0
    query = sql.SQL(
        """
        INSERT INTO {} (meta_id, start, mean, min, max, state, sum)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (meta_id, start) DO UPDATE SET
            mean = EXCLUDED.mean, min = EXCLUDED.min, max = EXCLUDED.max,
            state = EXCLUDED.state, sum = EXCLUDED.sum
        """
    ).format(sql.Identifier(table))
    with conn.cursor() as cur:
        cur.executemany(query, values)
    return len(values)


def position_marks(conn: psycopg.Connection) -> dict[str, datetime]:
    return dict(conn.execute('SELECT entity_id, max("time") FROM positions GROUP BY entity_id').fetchall())


def insert_positions(conn: psycopg.Connection, entity_id: str, rows: list[dict]) -> int:
    """Rows in HA's compressed history format: s = state, a = attributes, lu = last updated (epoch s)."""
    values = []
    for r in rows:
        a = r.get("a") or {}
        values.append((
            entity_id,
            datetime.fromtimestamp(r["lu"], timezone.utc),
            a.get("friendly_name"),
            r.get("s"),
            a.get("latitude"),
            a.get("longitude"),
            a.get("gps_accuracy"),
            a.get("source"),
        ))
    if not values:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO positions (entity_id, "time", name, state, latitude, longitude, accuracy, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (entity_id, "time") DO UPDATE SET
                name = EXCLUDED.name, state = EXCLUDED.state, latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude, accuracy = EXCLUDED.accuracy, source = EXCLUDED.source
            """,
            values,
        )
    return len(values)


def replace_google_positions(conn: psycopg.Connection, entity_id: str, name: str | None, rows) -> tuple[int, datetime | None]:
    """Swap an entity's imported Google rows for new ones. Rows from the moment HA's own data starts are left out."""
    cutoff = conn.execute(
        "SELECT min(\"time\") FROM positions WHERE entity_id = %s AND source IS DISTINCT FROM 'google'",
        (entity_id,),
    ).fetchone()[0]
    conn.execute("DELETE FROM positions WHERE entity_id = %s AND source = 'google'", (entity_id,))
    count = 0
    with conn.cursor() as cur:
        with cur.copy(
            'COPY positions (entity_id, "time", name, state, latitude, longitude, source) FROM STDIN'
        ) as copy:
            for t, state, lat, lon in rows:
                if cutoff and t >= cutoff:
                    break
                copy.write_row((entity_id, t, name, state, lat, lon, "google"))
                count += 1
    conn.commit()
    return count, cutoff


def set_home(conn: psycopg.Connection, latitude: float, longitude: float) -> None:
    conn.execute(
        "INSERT INTO heim (id, latitude, longitude) VALUES (1, %s, %s)"
        " ON CONFLICT (id) DO UPDATE SET latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude",
        (latitude, longitude),
    )
    conn.commit()


def fill_place_names(conn: psycopg.Connection, places) -> int:
    """Name every ~1 km square that positions reach and orte_namen does not know yet."""
    missing = conn.execute(
        """
        SELECT DISTINCT round(latitude::numeric, 2), round(longitude::numeric, 2) FROM positions p
        WHERE latitude IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM orte_namen o
            WHERE o.lat2 = round(p.latitude::numeric, 2) AND o.lon2 = round(p.longitude::numeric, 2))
        """
    ).fetchall()
    if not missing:
        return 0
    with conn.cursor() as cur:
        with cur.copy("COPY orte_namen (lat2, lon2, ort, land, km) FROM STDIN") as copy:
            for lat2, lon2 in missing:
                hit = places.nearest(float(lat2), float(lon2))
                copy.write_row((lat2, lon2, *(hit if hit else (None, None, None))))
    conn.commit()
    return len(missing)


def log_sync(conn: psycopg.Connection, rows: int, seconds: float) -> None:
    conn.execute(
        "INSERT INTO sync_log (finished, rows, seconds) VALUES (now(), %s, %s)",
        (rows, seconds),
    )
    conn.commit()
