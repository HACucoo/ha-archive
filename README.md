# ha-archive

Keeps Home Assistant's statistics for good, in PostgreSQL/TimescaleDB, with Grafana on top.

Home Assistant already stores hourly statistics (mean, min, max, sum) forever, but its 5-minute values only for the recorder's purge window (10 days by default). This container fetches both through the HA WebSocket API, once an hour, and keeps them in an archive of its own.

Because it pulls and does not get pushed to, the archive host may sleep. After a night, a holiday or a reboot it fetches whatever is missing, as long as HA still has it (10 days for 5-minute values, always for hourly ones). Home Assistant itself gets no extra write load.

- Every statistic HA keeps: all sensors with a `state_class`, energy meters, imported statistics
- Hourly values back to `HOURLY_SINCE`, 5-minute values from the first run on
- TimescaleDB compression (falls back to plain tables on stock PostgreSQL)
- Optional location history: positions and zones of the people you list, with a map of the route per day and the stays per place, as a self-hosted alternative to Google's timeline
- Grafana with a read-only database login, a provisioned datasource and two starter dashboards

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
| `LOCATION_ENTITIES` | — | comma-separated `person.*` (or `device_tracker.*`) for the location history |

## Data

| Table / view | |
|---|---|
| `meta` | one row per statistic: `statistic_id`, name, unit, `has_mean`, `has_sum` |
| `stats_hour`, view `stunde` | hourly rows |
| `stats_5min`, view `fuenf_minuten` | 5-minute rows |
| `positions` | one row per location update: `entity_id`, `time`, `name`, `state` (zone, `home`, `not_home`), `latitude`, `longitude`, `accuracy`, `source` |
| view `aufenthalte` | one row per stay: `name`, `ort`, `beginn`, `ende` |
| `heim`, `orte_namen` | home coordinates from HA's `zone.home`; nearest town per ~1 km square of the positions |
| view `heute_vor` | "on this day": per person and earlier year the place furthest from home on today's and tomorrow's date (at least 50 km away) |
| `sync_log` | one row per run |

Positions come from HA's state history, which the recorder purges after 10 days, so the archive starts on the day the importer first runs. Use `person.*` rather than the phone's tracker: the history then survives a new phone.

### Google timeline

Older history can come from Google Maps. Since 2024 the timeline lives on the phone: Settings → Location → Location services → Timeline → Export timeline data (on an iPhone: Google Maps → Settings → Personal content). Put the JSON into the import folder and run, once per person:

```bash
docker exec ha-archive-importer-1 python -m archiv --import-google /import/Zeitachse.json --entity person.stephan
```

Visits Google labels home or work become `Zuhause` and `Arbeit` (whatever the address was back then), other visits take the name of the HA zone they lie in, or `anderer Ort`. Trips are `unterwegs`. Rows from the moment HA's own data starts are left out, and importing again replaces the earlier import.

The statistics views carry `statistic_id`, `name`, `unit`, `time`, `mean`, `min`, `max`, `state`, `sum`. For meters, `sum` is the running total; consumption over a period is the difference of two `sum` values (see the "Verbrauch pro Tag" panel).

If statistics were corrected in HA afterwards (Developer tools → Statistics, `recorder.import_statistics`), fetch everything again:

```bash
docker exec ha-archive-importer-1 python -m archiv --once --resync
```

## Backup

Copying the database folder of a running PostgreSQL does not give a backup you can rely on. The `backup` service in the compose file writes a dump instead, `archiv.dump`, renewed when it is older than 20 hours. Back up that file. To restore into an empty database:

```bash
docker exec -i ha-archive-db-1 psql -U archiv -d archiv -c "SELECT timescaledb_pre_restore();"
docker exec -i ha-archive-db-1 pg_restore -U archiv -d archiv --no-owner < archiv.dump
docker exec -i ha-archive-db-1 psql -U archiv -d archiv -c "SELECT timescaledb_post_restore();"
```

## Place names

Town and country names come from [GeoNames](https://www.geonames.org/) (towns from 1000 inhabitants, German names where available), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The image carries them, so positions are named offline and never leave the archive host.

## Development

```bash
pip install -r requirements.txt pytest pytest-asyncio
PG_TEST_DSN=postgresql://postgres:test@localhost:5432/postgres pytest
```

Without `PG_TEST_DSN` the database tests are skipped; CI runs them against TimescaleDB.
