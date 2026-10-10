from datetime import datetime, timezone

import pytest

from archiv.ha import HAClient, HAError, ws_url


def test_ws_url():
    assert ws_url("http://192.168.2.139:8123") == "ws://192.168.2.139:8123/api/websocket"
    assert ws_url("https://ha.example") == "wss://ha.example/api/websocket"


async def test_auth_and_call(fake_ha):
    fake_ha.statistic_ids = [{"statistic_id": "sensor.a"}]
    async with HAClient(fake_ha.url, "good") as ha:
        assert await ha.statistic_ids() == [{"statistic_id": "sensor.a"}]


async def test_bad_token(fake_ha):
    with pytest.raises(HAError, match="authentication failed"):
        async with HAClient(fake_ha.url, "bad"):
            pass


async def test_error_result(fake_ha):
    async with HAClient(fake_ha.url, "good") as ha:
        with pytest.raises(HAError, match="unknown_command"):
            await ha.call("nope")


async def test_statistics_request(fake_ha):
    start = datetime(2026, 10, 10, 18, tzinfo=timezone.utc)
    fake_ha.rows[("sensor.a", "5minute")] = [{"start": start.timestamp() * 1000, "mean": 1.5}]
    async with HAClient(fake_ha.url, "good") as ha:
        result = await ha.statistics(["sensor.a"], start, datetime(2026, 10, 10, 19, tzinfo=timezone.utc), "5minute")
    assert result["sensor.a"][0]["mean"] == 1.5
    request = fake_ha.requests[-1]
    assert request["period"] == "5minute"
    assert request["start_time"] == "2026-10-10T18:00:00+00:00"
    assert set(request["types"]) == {"mean", "min", "max", "state", "sum"}
    assert request["units"] == {}
