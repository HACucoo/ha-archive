"""Entry point: python -m archiv [--once] [--resync]"""

import argparse
import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from . import db, google
from .config import Config
from .geo import Places
from .ha import HAClient
from .sync import sync_once

log = logging.getLogger("archiv")
HEALTH_FILE = Path("/tmp/healthy")
BUNDLED_PROVISIONING = Path(__file__).resolve().parent.parent / "grafana"


def install_provisioning(target: str | None) -> None:
    """Copy the Grafana datasource and dashboard into the volume Grafana reads at start."""
    if not target or not BUNDLED_PROVISIONING.is_dir():
        return
    shutil.copytree(BUNDLED_PROVISIONING, target, dirs_exist_ok=True)
    log.info("Grafana provisioning written to %s", target)


async def run(cfg: Config, once: bool, resync: bool) -> None:
    ready = False
    last = 0.0
    while True:
        # Wall clock, not asyncio's monotonic clock: that one stands still while the host is suspended,
        # and right after waking up is when there is most to fetch
        if time.time() - last < cfg.interval_s:
            await asyncio.sleep(60)
            continue
        last = time.time()
        try:
            with db.connect(cfg.pg_dsn) as conn:
                if not ready:
                    db.update_timescale(cfg.pg_dsn)
                    timescale = db.setup(conn, cfg.ro_password, cfg.private_password)
                    log.info("database ready (TimescaleDB: %s)", "yes" if timescale else "no")
                    ready = True
                async with HAClient(cfg.ha_url, cfg.ha_token) as ha:
                    await sync_once(ha, conn, cfg, resync=resync)
            resync = False
        except Exception:
            # HA restarting or the database not up yet: try again in five minutes
            log.exception("sync failed")
            last = time.time() - cfg.interval_s + 300
        if once:
            return


async def import_google(cfg: Config, path: str, entity_id: str) -> None:
    data = google.load(path)
    async with HAClient(cfg.ha_url, cfg.ha_token) as ha:
        states = await ha.states()
    zones = google.zones_from_states(states)
    person = next((s for s in states if s["entity_id"] == entity_id), None)
    if person is None:
        raise SystemExit(f"{entity_id} not found in Home Assistant")
    rows = google.rows(data, zones)
    with db.connect(cfg.pg_dsn) as conn:
        db.setup(conn, cfg.ro_password)
        count, cutoff = db.replace_google_positions(conn, entity_id, person["attributes"].get("friendly_name"), rows)
        if Places.available():
            log.info("named %d place squares", db.fill_place_names(conn, Places()))
    first = rows[0][0].date() if rows else None
    log.info("imported %d Google rows for %s from %s%s", count, entity_id, first,
             f", up to where HA's own data starts ({cutoff:%Y-%m-%d %H:%M})" if cutoff else "")


def main() -> None:
    parser = argparse.ArgumentParser(prog="archiv")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--resync", action="store_true",
                        help="fetch everything again, e.g. after corrected statistics in HA")
    parser.add_argument("--import-google", metavar="FILE",
                        help="import a Google Maps timeline export for --entity, then exit")
    parser.add_argument("--entity", help="person.* the Google export belongs to")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.import_google:
        if not args.entity:
            parser.error("--import-google needs --entity, e.g. --entity person.stephan")
        asyncio.run(import_google(Config.from_env(), args.import_google, args.entity))
        return
    # Grafana waits for this; a broken setting below must not keep it from starting
    install_provisioning(os.environ.get("PROVISIONING_DIR") or None)
    HEALTH_FILE.touch()
    try:
        cfg = Config.from_env()
    except SystemExit as err:
        if args.once:
            raise
        # Stay up (a restart loop would not fix the setting) until the stack is started with it
        log.error("%s – nothing is fetched until that is fixed and the importer restarted", err)
        while True:
            time.sleep(3600)
    asyncio.run(run(cfg, args.once, args.resync))


if __name__ == "__main__":
    main()
