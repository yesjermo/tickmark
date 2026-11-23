"""Connect TradingView: alerts from a user's own TradingView charts arrive here as signals.

TradingView has no sign-in API for other apps; the way traders connect it is an alert *webhook*. Each user gets a private URL
(/hooks/tradingview/<token>); they paste it into a TradingView alert with the JSON message template below. A signal lands in
the alerts feed and the TradingView signals list with an "Open ticket" button. It never places an order by itself.

The token is shown once (like an API key) and only its hash is stored, in portal.db, mapped to the user it belongs to.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timezone

from . import alerts, db, portal

MAX_BODY = 8 * 1024
RATE = (30, 60.0)                 # at most 30 alerts per minute per token
_hits: dict[str, list[float]] = {}
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-/:!]{0,23}$")
ACTIONS = {"buy", "sell", "long", "short", "exit", "close", "alert"}
TEMPLATE = ('{"symbol": "{{ticker}}", "action": "buy", "price": {{close}}, "interval": "{{interval}}", '
            '"note": "{{strategy.order.comment}}"}')
SCHEMA = """CREATE TABLE IF NOT EXISTS tv_signals (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, symbol TEXT NOT NULL,
            action TEXT NOT NULL, price REAL, interval TEXT, note TEXT, raw TEXT NOT NULL)"""


class HookError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def _h(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pconn():
    c = portal.conn()
    c.execute("CREATE TABLE IF NOT EXISTS tv_hooks (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)")
    return c


def status() -> dict:
    uid = portal.CURRENT_USER.get()
    c = _pconn()
    try:
        r = c.execute("SELECT created_at FROM tv_hooks WHERE user_id=?", (uid,)).fetchone()
    finally:
        c.close()
    with db.tx() as t:
        t.execute(SCHEMA)
        sig = [dict(x) for x in t.execute("SELECT id, at, symbol, action, price, interval, note FROM tv_signals ORDER BY id DESC LIMIT 50")]
    return {"connected": bool(r), "created_at": r["created_at"] if r else None, "signals": sig, "template": TEMPLATE}


def create_token() -> str:
    """A new private webhook token for the current user (replaces any earlier one)."""
    uid = portal.CURRENT_USER.get()
    token = "tvh_" + secrets.token_urlsafe(24)
    c = _pconn()
    try:
        c.execute("DELETE FROM tv_hooks WHERE user_id=?", (uid,))
        c.execute("INSERT INTO tv_hooks VALUES (?,?,?)", (_h(token), uid, _now()))
    finally:
        c.close()
    return token


def disconnect() -> None:
    c = _pconn()
    try:
        c.execute("DELETE FROM tv_hooks WHERE user_id=?", (portal.CURRENT_USER.get(),))
    finally:
        c.close()


def owner(token: str) -> str | None:
    if not token.startswith("tvh_") or len(token) > 80:
        return None
    c = _pconn()
    try:
        r = c.execute("SELECT user_id FROM tv_hooks WHERE token_hash=?", (_h(token),)).fetchone()
        return r["user_id"] if r else None
    finally:
        c.close()


def parse(raw: bytes) -> dict:
    if len(raw) > MAX_BODY:
        raise HookError("Alert message too long (8 KB max).", 413)
    text = raw.decode("utf-8", "replace").strip()
    try:
        body = json.loads(text)
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:             # a plain-text alert: "BUY NVDA 125.5" or any message that names a ticker first
        words = re.findall(r"[A-Za-z0-9.\-/:!]+", text)
        act = next((w.lower() for w in words if w.lower() in ACTIONS), "alert")
        sym = next((w for w in words if w.lower() not in ACTIONS and w.isupper() and SYMBOL.match(w)), "")
        body = {"symbol": sym, "action": act, "note": text[:500]}
    sym = str(body.get("symbol") or body.get("ticker") or "").upper().split(":")[-1]
    if not SYMBOL.match(sym or "?"):
        raise HookError("The alert needs a symbol, e.g. {\"symbol\": \"{{ticker}}\"}.", 422)
    act = str(body.get("action") or body.get("side") or "alert").lower()
    act = act if act in ACTIONS else "alert"
    try:
        price = float(body["price"]) if body.get("price") not in (None, "") else None
    except (TypeError, ValueError):
        price = None
    return {"symbol": sym, "action": act, "price": price, "interval": str(body.get("interval") or "")[:12] or None,
            "note": str(body.get("note") or body.get("message") or "")[:500] or None, "raw": text[:MAX_BODY]}


def _limited(token: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _hits.get(token, []) if now - t < RATE[1]]
    hits.append(now)
    _hits[token] = hits
    return len(hits) > RATE[0]


def receive(token: str, raw: bytes) -> dict:
    uid = owner(token)
    if not uid:
        raise HookError("Unknown webhook.", 404)
    if _limited(token):
        raise HookError("Too many alerts; slow down (30 a minute).", 429)
    sig = parse(raw)
    with portal.as_user(uid if uid != portal.LOCAL_USER else None):
        with db.tx() as c:
            c.execute(SCHEMA)
            c.execute("INSERT INTO tv_signals(at,symbol,action,price,interval,note,raw) VALUES(?,?,?,?,?,?,?)",
                      (_now(), sig["symbol"], sig["action"], sig["price"], sig["interval"], sig["note"], sig["raw"]))
            px = f" at {sig['price']:g}" if sig["price"] is not None else ""
            alerts.emit(c, "tradingview", f"TradingView: {sig['action']} {sig['symbol']}{px}",
                        (sig["note"] or "Alert from your TradingView chart.") + " Review it before trading; nothing was ordered.",
                        "info", sig["symbol"])
    return {"ok": True, "symbol": sig["symbol"], "action": sig["action"]}
