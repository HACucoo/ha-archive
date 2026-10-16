"""Settings, all from the environment."""

import os
import re
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
    state_entities: tuple[str, ...] = ()
    state_exclude: tuple[str, ...] = ()
    weather_history: bool = False

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        token = env.get("HA_TOKEN", "").strip()
        if not token:
            raise SystemExit("HA_TOKEN is missing (long-lived access token from your HA profile)")
        since = parse_date(env.get("HOURLY_SINCE", "2020-01-01"))
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
            location_entities=split(env.get("LOCATION_ENTITIES", "")),
            # Entity patterns (fnmatch, e.g. binary_sensor.*) whose state changes are archived
            state_entities=split(env.get("STATE_ENTITIES", "")),
            state_exclude=split(env.get("STATE_EXCLUDE", "")),
            # Daily mean temperature at home from Open-Meteo, e.g. for heating degree days
            weather_history=env.get("WEATHER_HISTORY", "").strip().lower() in ("1", "true", "yes", "on"),
        )


def split(raw: str) -> tuple[str, ...]:
    return tuple(e.strip() for e in raw.split(",") if e.strip())


def parse_date(raw: str) -> datetime:
    """ISO date; also takes what Compose makes of an unquoted YAML date ('2023-01-01 00:00:00 +0000 UTC')."""
    raw = raw.strip()
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        match = re.match(r"\d{4}-\d{2}-\d{2}", raw)
        if not match:
            raise SystemExit(f"HOURLY_SINCE is not a date: {raw!r}")
        value = datetime.fromisoformat(match.group())
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
