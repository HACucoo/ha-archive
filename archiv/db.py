"""PostgreSQL side: schema, metadata, upserts. TimescaleDB is used when available."""

import logging
from datetime import datetime, timezone

import psycopg
from psycopg import sql

log = logging.getLogger(__name__)

TABLES = {
    # table: (chunk interval, compress chunks older than)
    "stats_hour": ("90 days", "90 days"),
    "stats_5min": ("7 days", "14 days"),
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
"""


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn)


def setup(conn: psycopg.Connection, ro_password: str | None = None) -> bool:
    """Create tables and views; returns True when TimescaleDB is in use."""
    timescale = _try(conn, "CREATE EXTENSION IF NOT EXISTS timescaledb")
    conn.execute(SCHEMA)
    conn.commit()
    if timescale:
        for table, (chunk, compress_after) in TABLES.items():
            conn.execute(
                "SELECT create_hypertable(%s, by_range('start', %s::interval),"
                " if_not_exists => TRUE, migrate_data => TRUE)",
                (table, chunk),
            )
            enabled = conn.execute(
                "SELECT compression_enabled FROM timescaledb_information.hypertables"
                " WHERE hypertable_name = %s",
                (table,),
            ).fetchone()
            if enabled and not enabled[0]:
                conn.execute(sql.SQL(
                    "ALTER TABLE {} SET (timescaledb.compress,"
                    " timescaledb.compress_segmentby = 'meta_id', timescaledb.compress_orderby = 'start')"
                ).format(sql.Identifier(table)))
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
                name = EXCLUDED.name, unit = EXCLUDED.unit, has_mean = EXCLUDED.has_mean,
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


def log_sync(conn: psycopg.Connection, rows: int, seconds: float) -> None:
    conn.execute(
        "INSERT INTO sync_log (finished, rows, seconds) VALUES (now(), %s, %s)",
        (rows, seconds),
    )
    conn.commit()
