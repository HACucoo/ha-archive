# ha-archive

Keeps Home Assistant's statistics for good, in PostgreSQL/TimescaleDB, with Grafana on top.

Home Assistant already stores hourly statistics (mean, min, max, sum) forever, but its 5-minute values only for the recorder's purge window (10 days by default). This container fetches both through the HA WebSocket API, once an hour, and keeps them in an archive of its own.

Because it pulls and does not get pushed to, the archive host may sleep. After a night, a holiday or a reboot it fetches whatever is missing, as long as HA still has it (10 days for 5-minute values, always for hourly ones). Home Assistant itself gets no extra write load.

- Every statistic HA keeps: all sensors with a `state_class`, energy meters, imported statistics
- Hourly values back to `HOURLY_SINCE`, 5-minute values from the first run on
- TimescaleDB compression (falls back to plain tables on stock PostgreSQL)
- Grafana with a read-only database login, a provisioned datasource and a starter dashboard

## Run

Create a long-lived access token in Home Assistant (profile → Security → Long-lived access tokens), then:

```bash
mkdir -p /mnt/docker-data/ha-archiv/db /mnt/docker-data/ha-archiv/grafana
chown 1000:1000 /mnt/docker-data/ha-archiv/grafana
```

`.env` next to the compose file:

```
HA_TOKEN=...
DB_PASSWORD=...
GRAFANA_DB_PASSWORD=...
GRAFANA_ADMIN_PASSWORD=...
```

and the stack from [compose.example.yaml](compose.example.yaml). Grafana listens on port 3030; anyone on the LAN can look, editing needs the `admin` login.

The first run fetches the full hourly history and takes a few minutes. After that a run takes seconds.

## Settings

| Variable | Default | |
|---|---|---|
| `HA_URL` | `http://homeassistant.local:8123` | |
| `HA_TOKEN` | — | required |
| `PG_DSN` | `postgresql://archiv:archiv@db:5432/archiv` | |
| `GRAFANA_DB_PASSWORD` | — | creates/updates the read-only role `grafana_ro` |
| `INTERVAL_MINUTES` | `60` | |
| `HOURLY_SINCE` | `2020-01-01` | how far back the first run goes |
| `SHORT_TERM_DAYS` | `10` | HA's `purge_keep_days` |
| `PROVISIONING_DIR` | — | where to write the Grafana provisioning (shared volume) |

## Data

| Table / view | |
|---|---|
| `meta` | one row per statistic: `statistic_id`, name, unit, `has_mean`, `has_sum` |
| `stats_hour`, view `stunde` | hourly rows |
| `stats_5min`, view `fuenf_minuten` | 5-minute rows |
| `sync_log` | one row per run |

The views carry `statistic_id`, `name`, `unit`, `time`, `mean`, `min`, `max`, `state`, `sum`. For meters, `sum` is the running total; consumption over a period is the difference of two `sum` values (see the "Verbrauch pro Tag" panel).

If statistics were corrected in HA afterwards (Developer tools → Statistics, `recorder.import_statistics`), fetch everything again:

```bash
docker exec ha-archive-importer-1 python -m archiv --once --resync
```

## Development

```bash
pip install -r requirements.txt pytest pytest-asyncio
PG_TEST_DSN=postgresql://postgres:test@localhost:5432/postgres pytest
```

Without `PG_TEST_DSN` the database tests are skipped; CI runs them against TimescaleDB.
