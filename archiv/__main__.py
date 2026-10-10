"""Entry point: python -m archiv [--once] [--resync]"""

import argparse
import asyncio
import logging
import shutil
from pathlib import Path

from . import db, google
from .config import Config
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
    while True:
        try:
            with db.connect(cfg.pg_dsn) as conn:
                if not ready:
                    timescale = db.setup(conn, cfg.ro_password)
                    log.info("database ready (TimescaleDB: %s)", "yes" if timescale else "no")
                    ready = True
                async with HAClient(cfg.ha_url, cfg.ha_token) as ha:
                    await sync_once(ha, conn, cfg, resync=resync)
            resync = False
        except Exception:
            # HA restarting or the database not up yet: try again next round
            log.exception("sync failed")
        if once:
            return
        await asyncio.sleep(cfg.interval_s)


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
    cfg = Config.from_env()
    if args.import_google:
        if not args.entity:
            parser.error("--import-google needs --entity, e.g. --entity person.stephan")
        asyncio.run(import_google(cfg, args.import_google, args.entity))
        return
    install_provisioning(cfg.provisioning_dir)
    HEALTH_FILE.touch()
    asyncio.run(run(cfg, args.once, args.resync))


if __name__ == "__main__":
    main()
