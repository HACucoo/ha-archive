"""Entry point: python -m archiv [--once] [--resync]"""

import argparse
import asyncio
import logging
import shutil
from pathlib import Path

from . import db
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="archiv")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--resync", action="store_true",
                        help="fetch everything again, e.g. after corrected statistics in HA")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    install_provisioning(cfg.provisioning_dir)
    HEALTH_FILE.touch()
    asyncio.run(run(cfg, args.once, args.resync))


if __name__ == "__main__":
    main()
