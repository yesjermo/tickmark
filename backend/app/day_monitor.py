"""Server-side day-trading monitor.

While the server runs in Live data mode, every ~30 s during market hours it:
  * advances the live intraday session for simulator day trades (the same work the
    Day Trading tab does when it's open, so a closed browser tab doesn't stall fills);
  * syncs broker day trades (fills of the bracket entry, stop and take-profit legs);
  * from the flat-by time (default 15:55 ET), cancels unfilled broker day-trade entries
    and sells every broker day-trade position at market.
After 16:00 ET, a broker day-trade position that is still open raises a critical alert
(Alpaca's DAY legs have expired by then, so the position is unprotected overnight).

SWINGDESK_DAYMON=off never starts the thread (used by the tests); `tick(now)` is the
testable unit.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, time

from . import alerts, db, events, intraday
from .brokers import simulator as sim
from .brokers.base import OPEN_STATUSES
from .tradingcal import is_session

INTERVAL = 30
OPEN_T, CLOSE_T = time(9, 30), time(16, 0)


def enabled() -> bool:
    return os.environ.get("SWINGDESK_DAYMON", "on") != "off"


def _broker_day_entries() -> list[dict]:
    c = db.connect()
    try:
        out = []
        for r in c.execute("""SELECT * FROM orders WHERE broker!='simulator' AND style='intraday' AND purpose='entry'
                              ORDER BY created_at"""):
            r = dict(r)
            r["held"] = sim._open_qty(c, r["id"])
            working_exit = c.execute("""SELECT 1 FROM orders WHERE parent_id=? AND purpose IN ('flat','manual_exit')
                                        AND status IN ('pending','partially_filled')""", (r["id"],)).fetchone()
            r["exit_working"] = bool(working_exit)
            if r["held"] > 0 or r["status"] in OPEN_STATUSES:
                out.append(r)
        return out
    finally:
        c.close()


def tick(now: datetime | None = None) -> list[str]:
    """One monitor pass. Returns human-readable events."""
    from . import broker_sync, day
    from .trading import TradeError, cancel_order
    now = now or intraday.now_et()
    if db.data_mode() != "live" or not is_session(now.date()):
        return []
    out: list[str] = []
    if OPEN_T <= now.time() < CLOSE_T:
        try:
            r = day.sync_live()
            out += r.get("events", [])
        except Exception as exc:                      # data hiccup: try again next tick
            out.append(f"sim sync failed: {type(exc).__name__}: {exc}"[:160])
    entries = _broker_day_entries()
    if not entries:
        return out
    try:
        broker_sync.sync()
    except Exception as exc:
        out.append(f"broker sync failed: {type(exc).__name__}: {exc}"[:160])
    entries = _broker_day_entries()
    c = db.connect()
    try:
        p, _ = intraday.active_params(c)
    finally:
        c.close()
    flat_by = time.fromisoformat(p.flat_by)
    if flat_by <= now.time() < CLOSE_T:
        for e in entries:
            try:
                if e["status"] in OPEN_STATUSES and e["held"] <= 0:
                    cancel_order(e["id"])
                    out.append(f"{e['symbol']}: unfilled day-trade entry canceled at flat-by")
                elif e["held"] > 0 and not e["exit_working"]:
                    broker_sync.flatten_day_trade(e["id"], "flat", f"flat by {p.flat_by} (market)")
                    out.append(f"{e['symbol']}: flattened {e['held']} at flat-by")
            except TradeError as exc:
                out.append(f"{e['symbol']}: flatten failed: {exc}")
                _alert(f"Day trade NOT flattened: {e['symbol']}", f"{exc}. Close it in your broker account.",
                       e["symbol"], f"dayflat:{now.date()}:{e['id']}")
    elif now.time() >= CLOSE_T:
        for e in entries:
            if e["held"] > 0:
                _alert(f"Day trade still open after the close: {e['symbol']}",
                       f"{e['held']} shares are still held at the broker and its DAY stop/target have expired. Close it in your broker account.",
                       e["symbol"], f"dayopen:{now.date()}:{e['id']}")
                out.append(f"{e['symbol']}: still open after close")
    if out:
        events.bump("day monitor")
    return out


def _alert(title: str, body: str, symbol: str, key: str) -> None:
    with db.tx() as conn:
        alerts.emit(conn, "day", title, body, "critical", symbol, key)
    alerts.flush()


class DayMonitor:
    def __init__(self, clock=None):
        self.clock = clock or intraday.now_et
        self.stop_ev = threading.Event()
        self.thread: threading.Thread | None = None
        self.last: dict = {}
        self.last_crypto: dict = {}

    def start(self):
        if not enabled() or self.thread:
            return
        self.thread = threading.Thread(target=self._run, name="day-monitor", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_ev.set()

    def _run(self):
        from . import portal
        while not self.stop_ev.is_set():
            now = self.clock()
            portal.for_each_tenant(lambda: self._one(now))   # every portal user, or the single-user app
            self.stop_ev.wait(INTERVAL)

    def _one(self, now):
        self._crypto()
        from . import portal
        if portal.enabled() and db.data_mode() == "live":      # the portal has no order websocket: poll broker orders instead
            try:
                from . import broker_sync
                broker_sync.sync()
            except Exception:
                pass
        if is_session(now.date()) and OPEN_T <= now.time() <= time(16, 5):
            try:
                ev = tick(now)
                self.last = {"at": now.isoformat(timespec="seconds"), "events": ev[-10:]}
            except Exception as exc:
                self.last = {"at": now.isoformat(timespec="seconds"), "error": f"{type(exc).__name__}: {exc}"[:200]}

    def _crypto(self):
        """Crypto trades 24/7: sync working crypto orders (and place stops for filled buys) every tick."""
        from . import crypto_trading
        try:
            if db.data_mode() != "live":
                return
            c = db.connect()
            try:
                n = c.execute("SELECT COUNT(*) FROM crypto_orders WHERE status IN ('pending','open','partially_filled') "
                              "OR (purpose='entry' AND status='filled' AND stop_price IS NOT NULL AND NOT EXISTS "
                              "(SELECT 1 FROM crypto_orders s WHERE s.parent_id=crypto_orders.id AND s.purpose='stop'))").fetchone()[0]
            finally:
                c.close()
            if n:
                self.last_crypto = crypto_trading.sync()
        except Exception as exc:
            self.last_crypto = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    def state(self) -> dict:
        return {"enabled": enabled(), "thread_alive": bool(self.thread and self.thread.is_alive()), **self.last,
                "crypto": self.last_crypto}
