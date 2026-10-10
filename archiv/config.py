"""Settings, all from the environment."""

import os
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Config:
    ha_url: str
    ha_token: str
    pg_dsn: str
    ro_password: str | None
    interval_s: int
    hourly_since: datetime
    short_days: int
    provisioning_dir: str | None
    location_entities: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        token = env.get("HA_TOKEN", "").strip()
        if not token:
            raise SystemExit("HA_TOKEN is missing (long-lived access token from your HA profile)")
        since = datetime.fromisoformat(env.get("HOURLY_SINCE", "2020-01-01"))
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        return cls(
            ha_url=env.get("HA_URL", "http://homeassistant.local:8123").rstrip("/"),
            ha_token=token,
            pg_dsn=env.get("PG_DSN", "postgresql://archiv:archiv@db:5432/archiv"),
            ro_password=env.get("GRAFANA_DB_PASSWORD") or None,
            interval_s=int(env.get("INTERVAL_MINUTES", "60")) * 60,
            hourly_since=since,
            # HA keeps 5-minute statistics as long as its recorder keeps states (10 days by default)
            short_days=int(env.get("SHORT_TERM_DAYS", "10")),
            provisioning_dir=env.get("PROVISIONING_DIR") or None,
            # person.* (or device_tracker.*) whose positions and zones are archived
            location_entities=tuple(e.strip() for e in env.get("LOCATION_ENTITIES", "").split(",") if e.strip()),
        )
