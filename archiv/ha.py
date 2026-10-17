"""Minimal Home Assistant WebSocket client: auth plus request/response."""

import asyncio
import json
from datetime import datetime, timezone

import websockets

TYPES = ["mean", "min", "max", "state", "sum"]


class HAError(Exception):
    pass


def ws_url(base: str) -> str:
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + "/api/websocket"
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + "/api/websocket"
    return base


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class HAClient:
    def __init__(self, url: str, token: str, timeout: float = 120):
        self.url = ws_url(url)
        self.token = token
        self.timeout = timeout
        self.ws = None
        self._id = 0

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, max_size=None, open_timeout=20)
        try:
            hello = json.loads(await self._recv())
            if hello.get("type") != "auth_required":
                raise HAError(f"unexpected greeting: {hello.get('type')}")
            await self.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            answer = json.loads(await self._recv())
            if answer.get("type") != "auth_ok":
                raise HAError(f"authentication failed: {answer.get('message', answer.get('type'))}")
        except BaseException:
            # __aexit__ does not run when __aenter__ fails: without this every failed attempt leaves a socket open
            await self.ws.close()
            raise
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def _recv(self):
        return await asyncio.wait_for(self.ws.recv(), self.timeout)

    async def call(self, msg_type: str, **data):
        self._id += 1
        await self.ws.send(json.dumps({"id": self._id, "type": msg_type, **data}))
        while True:
            msg = json.loads(await self._recv())
            if msg.get("id") != self._id:
                continue
            if not msg.get("success"):
                err = msg.get("error") or {}
                raise HAError(f"{msg_type}: {err.get('code')} {err.get('message')}")
            return msg.get("result")

    async def states(self) -> list[dict]:
        return await self.call("get_states")

    async def statistic_ids(self) -> list[dict]:
        return await self.call("recorder/list_statistic_ids")

    async def statistics(self, ids: list[str], start: datetime, end: datetime, period: str) -> dict:
        return await self.call(
            "recorder/statistics_during_period",
            statistic_ids=ids,
            start_time=iso(start),
            end_time=iso(end),
            period=period,
            types=TYPES,
            units={},
        ) or {}

    async def history(self, entity_ids: list[str], start: datetime, end: datetime) -> dict:
        """Every state row with attributes, in HA's compressed format (s, a, lu, lc)."""
        return await self.call(
            "history/history_during_period",
            entity_ids=entity_ids,
            start_time=iso(start),
            end_time=iso(end),
            minimal_response=False,
            no_attributes=False,
            significant_changes_only=False,
            # Otherwise HA adds a made-up row at start_time with the state of that moment
            include_start_time_state=False,
        ) or {}

    async def state_changes(self, entity_ids: list[str], start: datetime, end: datetime) -> dict:
        """Only state changes, no attributes: rows (s, lu), the first row of an entity also has a and lc."""
        return await self.call(
            "history/history_during_period",
            entity_ids=entity_ids,
            start_time=iso(start),
            end_time=iso(end),
            minimal_response=True,
            no_attributes=True,
            significant_changes_only=True,
            include_start_time_state=False,
        ) or {}
