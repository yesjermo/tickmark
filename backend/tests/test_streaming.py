"""Streaming order updates against a real local websocket server that speaks Alpaca's
trade_updates protocol (auth -> authorized, listen -> listening, binary JSON events)."""
import json
import queue
import threading
import time

import pytest
from websockets.sync.server import serve

from app import broker_sync, credentials, events, streaming
from app.brokers import alpaca as alpaca_mod
from fake_alpaca import GOOD_KEY, GOOD_SECRET, FakeAlpaca
from test_live_forward import confirm, go_to, live, world  # noqa: F401  (fixtures)


class FakeStream:
    """Local websocket server. Push events with .push(); .kick() drops all clients."""
    def __init__(self, key=GOOD_KEY, secret=GOOD_SECRET):
        self.key, self.secret = key, secret
        self.q: queue.Queue = queue.Queue()
        self.conns, self.auths = [], []
        self.stopping = threading.Event()
        self.server = serve(self.handler, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/stream"

    def handler(self, ws):
        self.conns.append(ws)
        auth = json.loads(ws.recv())
        self.auths.append(auth)
        ok = auth.get("action") == "auth" and auth.get("key") == self.key and auth.get("secret") == self.secret
        ws.send(json.dumps({"stream": "authorization", "data": {"status": "authorized" if ok else "unauthorized",
                                                                "action": "authenticate"}}).encode())
        if not ok:
            return
        listen = json.loads(ws.recv())
        assert listen == {"action": "listen", "data": {"streams": ["trade_updates"]}}
        ws.send(json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}}).encode())   # binary frame
        while not self.stopping.is_set():
            try:
                ev = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            if ev == "KICK":
                return
            ws.send(json.dumps(ev).encode())

    def push(self, event, order, **extra):
        self.q.put({"stream": "trade_updates", "data": {"event": event, "order": order,
                                                        "timestamp": "2026-11-23T14:31:00Z", **extra}})

    def kick(self):
        self.q.put("KICK")

    def close(self):
        self.stopping.set()          # let handler loops end so shutdown() can return
        for c in self.conns:
            try:
                c.close()
            except Exception:
                pass
        self.server.shutdown()


def wait_for(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def setup(live, monkeypatch):
    fake = FakeAlpaca()
    monkeypatch.setattr(alpaca_mod, "TRANSPORT", fake.transport())
    credentials.save_alpaca(GOOD_KEY, GOOD_SECRET)
    assert live.put("/api/broker/mode", json={"mode": "alpaca_paper"}).status_code == 200
    confirm(live)
    fs = FakeStream()
    monkeypatch.setattr(streaming, "STREAM_URL_OVERRIDE", fs.url)
    s = streaming.OrderStream()
    yield live, fake, fs, s
    s.stop()
    fs.close()


def place(live):
    pv = live.post("/api/previews", json={"symbol": "NVDA"}).json()
    o = live.post("/api/orders", json={"preview_id": pv["id"], "confirm": True}).json()["order"]
    return pv, o


def local(live, oid):
    return next(x for x in live.get("/api/orders").json() if x["id"] == oid)


def test_connects_with_documented_auth_and_listen(setup):
    live, fake, fs, s = setup
    s.start(force=True)
    assert wait_for(lambda: s.state["connected"] and s.state["transport"] == "websocket")
    assert fs.auths[0] == {"action": "auth", "key": GOOD_KEY, "secret": GOOD_SECRET}


def test_fill_event_updates_order_journal_and_alerts_without_polling(setup, monkeypatch):
    live, fake, fs, s = setup
    pv, o = place(live)
    s.start(force=True)
    assert wait_for(lambda: s.state["connected"])
    # From here on, REST polling is disabled: only the pushed event can update the order.
    monkeypatch.setattr(broker_sync, "sync", lambda *a, **k: {"synced": 0, "errors": []})
    v0, _ = events.current()
    fake.fill_entry(pv["id"], 168.25)
    fs.push("fill", fake._view(fake.by_client(pv["id"])), price="168.25", qty=str(pv["qty"]), position_qty=str(pv["qty"]))
    assert wait_for(lambda: s.state["events"] >= 1)
    conn = __import__("app.db", fromlist=["x"]).connect()
    row = conn.execute("SELECT status, avg_fill_price FROM orders WHERE id=?", (o["id"],)).fetchone()
    stop = conn.execute("SELECT status FROM orders WHERE parent_id=? AND purpose='stop'", (o["id"],)).fetchone()
    j = conn.execute("SELECT entry_price, entry_date FROM journal_outcomes").fetchone()
    kinds = [r[0] for r in conn.execute("SELECT kind FROM alerts")]
    conn.close()
    assert row["status"] == "filled" and row["avg_fill_price"] == 168.25
    assert stop and stop["status"] == "pending"          # the stop leg arrived inside the same event
    assert j["entry_price"] == 168.25 and j["entry_date"] == "2026-11-23"
    assert "fill" in kinds
    assert events.current()[0] > v0                     # the UI is told to refresh


def test_stop_leg_fill_closes_trade_via_stream(setup, monkeypatch):
    live, fake, fs, s = setup
    pv, o = place(live)
    fake.fill_entry(pv["id"], 168.0)
    live.get("/api/orders")                               # stop leg row exists locally
    s.start(force=True)
    assert wait_for(lambda: s.state["connected"])
    monkeypatch.setattr(broker_sync, "sync", lambda *a, **k: {"synced": 0, "errors": []})
    fake.trigger_stop("NVDA", pv["stop_price"] - 0.4)
    leg = next(x for x in fake.orders.values() if x["type"] == "stop")
    fs.push("fill", dict(leg))
    assert wait_for(lambda: s.state["events"] >= 1)
    assert wait_for(lambda: live.get("/api/journal").json()[0]["status"] == "closed")
    j = live.get("/api/journal").json()[0]
    assert j["exit_reason"] == "stop" and j["realized_pnl"] < 0


def test_unknown_order_triggers_resync(setup, monkeypatch):
    live, fake, fs, s = setup
    calls = []
    real = broker_sync.sync
    monkeypatch.setattr(broker_sync, "sync", lambda *a, **k: calls.append(1) or real(*a, **k))
    s.start(force=True)
    assert wait_for(lambda: s.state["connected"])
    n = len(calls)
    fs.push("new", {"id": "not-ours", "client_order_id": "manual-in-app", "symbol": "MSFT", "status": "new",
                    "qty": "1", "filled_qty": "0", "side": "buy", "type": "market"})
    assert wait_for(lambda: len(calls) > n)


def test_reconnects_and_catches_up_after_drop(setup, monkeypatch):
    live, fake, fs, s = setup
    syncs = []
    real = broker_sync.sync
    monkeypatch.setattr(broker_sync, "sync", lambda *a, **k: syncs.append(1) or real(*a, **k))
    monkeypatch.setattr(streaming, "RECV_TIMEOUT", 0.5)
    s.start(force=True)
    assert wait_for(lambda: s.state["connected"])
    first = len(syncs)
    fs.kick()
    assert wait_for(lambda: s.state["reconnects"] >= 1)
    assert wait_for(lambda: s.state["connected"] and len(syncs) > first, timeout=12)   # gap-fill sync on reconnect


def test_bad_keys_reported(setup, monkeypatch):
    live, fake, fs, s = setup
    fs.key = "someone-else"
    s.start(force=True)
    assert wait_for(lambda: s.state["last_error"] and "refused" in s.state["last_error"])
    assert s.state["connected"] is False


def test_ibkr_uses_fast_polling(client, monkeypatch):
    s = streaming.OrderStream()
    calls = []
    monkeypatch.setattr(broker_sync, "sync", lambda *a, **k: calls.append(1) or {"synced": 0, "errors": []})
    s.poll_once()
    assert calls == [1] and s.state["last_sync_at"]


def test_stream_idle_without_broker(client):
    assert streaming._target() is None


def test_sse_endpoint_streams_changes(client):
    import asyncio
    from app import main

    class Req:
        def __init__(self):
            self.n = 0

        async def is_disconnected(self):
            self.n += 1
            return self.n > 3

    async def run():
        resp = await main.stream_changes(Req())
        it = resp.body_iterator.__aiter__()
        first = await it.__anext__()
        chunks = [first if isinstance(first, str) else first.decode()]
        events.bump("test change")            # a change after the client connected
        async for c in it:
            chunks.append(c if isinstance(c, str) else c.decode())
        return chunks

    chunks = asyncio.run(run())
    assert chunks[0].startswith("event: hello") and any("test change" in c for c in chunks)
