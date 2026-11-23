"""Real-time order updates.

Alpaca: a websocket to the trade_updates stream (wss://paper-api.alpaca.markets/stream, or
wss://api.alpaca.markets/stream behind the live gate). Protocol per Alpaca's docs: send
{"action":"auth","key","secret"}, expect {"stream":"authorization","data":{"status":"authorized"}},
then {"action":"listen","data":{"streams":["trade_updates"]}}. Each event carries the full order
object, which is applied straight to the local order rows; fills, stop-outs and exits reach the
journal, alerts and the UI within seconds. Frames arrive as binary JSON.

On every (re)connect a full REST sync runs first, so anything that happened while disconnected
is caught up; polling remains the fallback and the basis for reconciliation.

Interactive Brokers: fast polling (every 10 s during market hours, 5 min otherwise). Its
websocket order topic needs gateway session cookies and is a later step.

Runs only in Live data mode with an external broker connected. SWINGDESK_STREAM=off disables it.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

from . import alerts, broker_sync, credentials, db, events, live_data
from .brokers import alpaca, factory
from .tradingcal import is_session

PAPER_STREAM = "wss://paper-api.alpaca.markets/stream"
LIVE_STREAM = "wss://api.alpaca.markets/stream"
STREAM_URL_OVERRIDE: str | None = None     # tests point this at a local server
POLL_MARKET, POLL_IDLE = 10, 300
RECV_TIMEOUT = 5


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _target() -> str | None:
    """Which broker to follow right now, or None."""
    if db.data_mode() != "live":
        return None
    try:
        c = db.connect()
        try:
            db.init_schema(c)
            mode = db.kv_get(c, "broker_mode", "simulator")
        finally:
            c.close()
    except Exception:
        return None
    return mode if factory.is_external(mode) and factory.connected(mode) else None


def market_hours(now: datetime | None = None) -> bool:
    now = now or live_data.now_et()
    return is_session(now.date()) and (9, 25) <= (now.hour, now.minute) <= (16, 15)


class OrderStream:
    def __init__(self):
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = {"target": None, "transport": None, "connected": False, "last_event_at": None, "events": 0,
                      "last_error": None, "reconnects": 0, "last_sync_at": None}

    # ---- event handling ----
    def handle_message(self, raw) -> str | None:
        msg = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
        if msg.get("action") == "error" or msg.get("stream") == "error":
            raise RuntimeError(str(msg.get("data", {}).get("error_message") or msg))
        if msg.get("stream") != "trade_updates":
            return None
        data = msg.get("data") or {}
        order = data.get("order") or {}
        if not order.get("id"):
            return None
        res = alpaca.AlpacaPaperBroker._result(order)
        ts = data.get("timestamp") or order.get("updated_at")
        try:
            session = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(live_data.ET).date().isoformat()
        except Exception:
            session = live_data.now_et().date().isoformat()
        outcome = broker_sync.apply_event("alpaca_paper", res, order.get("client_order_id"), session)
        self.state.update(last_event_at=_now(), events=self.state["events"] + 1)
        alerts.flush()
        events.bump(f"order {data.get('event')} {order.get('symbol')}")
        return outcome

    def _gap_fill(self):
        try:
            broker_sync.sync()
            self.state["last_sync_at"] = _now()
            events.bump("sync")
            alerts.flush()
        except Exception as exc:
            self.state["last_error"] = f"sync: {type(exc).__name__}: {exc}"[:200]

    # ---- Alpaca websocket ----
    def stream_url(self) -> str:
        if STREAM_URL_OVERRIDE:
            return STREAM_URL_OVERRIDE
        return LIVE_STREAM if db.environment() == "real" else PAPER_STREAM

    def run_alpaca(self) -> None:
        from websockets.sync.client import connect
        key, secret, _ = credentials.get_alpaca(db.environment())
        with connect(self.stream_url(), open_timeout=10, close_timeout=3) as ws:
            ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
            auth = json.loads(_text(ws.recv(timeout=10)))
            if (auth.get("data") or {}).get("status") != "authorized":
                raise PermissionError(f"Alpaca stream refused the keys: {auth}")
            ws.send(json.dumps({"action": "listen", "data": {"streams": ["trade_updates"]}}))
            ack = json.loads(_text(ws.recv(timeout=10)))
            if "trade_updates" not in ((ack.get("data") or {}).get("streams") or []):
                raise RuntimeError(f"trade_updates subscription not acknowledged: {ack}")
            self.state.update(connected=True, transport="websocket", last_error=None)
            events.bump("stream connected")
            self._gap_fill()   # catch up on anything missed while disconnected
            while not self._stop.is_set():
                if _target() != "alpaca_paper":   # broker switched or data mode changed
                    self.state.update(connected=False, transport=None, target=_target())
                    events.bump("stream stopped")
                    return
                try:
                    raw = ws.recv(timeout=RECV_TIMEOUT)
                except TimeoutError:
                    continue
                self.handle_message(raw)

    # ---- polling (IBKR and fallback) ----
    def poll_once(self) -> None:
        self._gap_fill()

    # ---- loop ----
    def _loop(self):
        backoff = 2
        while not self._stop.is_set():
            target = _target()
            self.state["target"] = target
            if target is None:
                self.state.update(connected=False, transport=None)
                self._stop.wait(5)
                continue
            if target == "alpaca_paper":
                try:
                    self.run_alpaca()
                    backoff = 2
                except Exception as exc:
                    self.state.update(connected=False, last_error=f"{type(exc).__name__}: {exc}"[:200],
                                      reconnects=self.state["reconnects"] + 1)
                    events.bump("stream disconnected")
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 60)
                continue
            self.state.update(transport="poll", connected=True)
            self.poll_once()
            self._stop.wait(POLL_MARKET if market_hours() else POLL_IDLE)

    def start(self, force: bool = False):
        if not force and os.environ.get("SWINGDESK_STREAM", "on").lower() in ("off", "0", "false"):
            return
        self.thread = threading.Thread(target=self._loop, name="swingdesk-orderstream", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()


def _text(raw):
    return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
