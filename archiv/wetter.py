"""Daily temperature at home from Open-Meteo's historical weather API (reanalysis, no key needed)."""

import json
import logging
import urllib.parse
import urllib.request
from datetime import date

log = logging.getLogger(__name__)

URL = "https://archive-api.open-meteo.com/v1/archive"
DAILY = "temperature_2m_mean,temperature_2m_min,temperature_2m_max"


def fetch(latitude: float, longitude: float, start: date, end: date) -> list[tuple]:
    """(day, mean, min, max) per day. The position goes out rounded to 0.1° (about 10 km),
    which is finer than the reanalysis grid anyway."""
    query = urllib.parse.urlencode({
        "latitude": round(latitude, 1),
        "longitude": round(longitude, 1),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": DAILY,
        "timezone": "auto",
    })
    with urllib.request.urlopen(f"{URL}?{query}", timeout=60) as response:
        data = json.load(response)
    return rows(data)


def rows(data: dict) -> list[tuple]:
    daily = data.get("daily") or {}
    return [
        (date.fromisoformat(day), mean, low, high)
        for day, mean, low, high in zip(
            daily.get("time", []),
            daily.get("temperature_2m_mean", []),
            daily.get("temperature_2m_min", []),
            daily.get("temperature_2m_max", []),
        )
    ]
