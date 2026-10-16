from datetime import datetime, timezone

import pytest

from archiv.config import Config, parse_date

JAN = datetime(2023, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw", ["2023-01-01", "2023-01-01T00:00:00+00:00", "2023-01-01 00:00:00 +0000 UTC"])
def test_parse_date(raw):
    assert parse_date(raw) == JAN


def test_not_a_date():
    with pytest.raises(SystemExit):
        parse_date("gestern")


def test_from_env():
    cfg = Config.from_env({"HA_TOKEN": "t", "HOURLY_SINCE": "2023-01-01 00:00:00 +0000 UTC",
                           "LOCATION_ENTITIES": "person.a, person.b,"})
    assert cfg.hourly_since == JAN
    assert cfg.location_entities == ("person.a", "person.b")


def test_state_and_weather_settings():
    cfg = Config.from_env({"HA_TOKEN": "t", "STATE_ENTITIES": "binary_sensor.*, light.*",
                           "STATE_EXCLUDE": "binary_sensor.*_update", "WEATHER_HISTORY": "True"})
    assert cfg.state_entities == ("binary_sensor.*", "light.*")
    assert cfg.state_exclude == ("binary_sensor.*_update",)
    assert cfg.weather_history is True
    assert Config.from_env({"HA_TOKEN": "t"}).weather_history is False
