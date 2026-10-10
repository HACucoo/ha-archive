import json
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeHA:
    """Speaks just enough of the HA WebSocket API; records every request."""

    def __init__(self, token="good"):
        self.token = token
        self.requests = []
        self.statistic_ids = []
        self.rows = {}  # (statistic_id, period) -> list of rows with start in ms
        self.history = {}  # entity_id -> list of compressed state rows (s, a, lu)

    async def handler(self, ws):
        await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.9.1"}))
        auth = json.loads(await ws.recv())
        if auth.get("access_token") != self.token:
            await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
            return
        await ws.send(json.dumps({"type": "auth_ok"}))
        async for raw in ws:
            msg = json.loads(raw)
            self.requests.append(msg)
            # Unrelated event first, the client has to skip it
            await ws.send(json.dumps({"id": 999, "type": "event", "event": {}}))
            await ws.send(json.dumps(self.answer(msg)))

    def answer(self, msg):
        if msg["type"] == "recorder/list_statistic_ids":
            return {"id": msg["id"], "type": "result", "success": True, "result": self.statistic_ids}
        if msg["type"] == "recorder/statistics_during_period":
            from datetime import datetime

            start = datetime.fromisoformat(msg["start_time"]).timestamp() * 1000
            end = datetime.fromisoformat(msg["end_time"]).timestamp() * 1000
            result = {}
            for sid in msg["statistic_ids"]:
                rows = [r for r in self.rows.get((sid, msg["period"]), []) if start <= r["start"] < end]
                if rows:
                    result[sid] = rows
            return {"id": msg["id"], "type": "result", "success": True, "result": result}
        if msg["type"] == "history/history_during_period":
            from datetime import datetime

            start = datetime.fromisoformat(msg["start_time"]).timestamp()
            end = datetime.fromisoformat(msg["end_time"]).timestamp()
            result = {}
            for eid in msg["entity_ids"]:
                rows = [r for r in self.history.get(eid, []) if start <= r["lu"] < end]
                if rows:
                    result[eid] = rows
            return {"id": msg["id"], "type": "result", "success": True, "result": result}
        return {"id": msg["id"], "type": "result", "success": False,
                "error": {"code": "unknown_command", "message": "Unknown command."}}


@pytest_asyncio.fixture
async def fake_ha():
    ha = FakeHA()
    async with websockets.serve(ha.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ha.url = f"http://127.0.0.1:{port}"
        yield ha


@pytest.fixture
def pg():
    """Fresh database per test: PG_TEST_DSN, or an embedded server via pgserver if installed."""
    import psycopg

    dsn = os.environ.get("PG_TEST_DSN")
    server = None
    if not dsn:
        try:
            import pgserver
        except ImportError:
            pytest.skip("no PostgreSQL for tests (set PG_TEST_DSN or pip install pgserver)")
        server = pgserver.get_server(Path(__file__).resolve().parent / ".pgdata", cleanup_mode="stop")
        dsn = server.get_uri()
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS archiv_test WITH (FORCE)")
        admin.execute("CREATE DATABASE archiv_test")
    conn = psycopg.connect(psycopg.conninfo.make_conninfo(dsn, dbname="archiv_test"))
    yield conn
    conn.close()
