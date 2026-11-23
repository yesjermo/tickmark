"""Local paper-broker simulator. Works with no credentials.

Fill model (stated in the UI and the README):
  * Orders submitted after the close wait for the next simulated session.
  * Market entries fill at that session's open plus SLIPPAGE_BPS.
  * Limit buys fill at min(open, limit) if the session low reaches the limit.
  * Entry fills are capped at PARTICIPATION of the session's volume; any
    remainder stays working (partially_filled) and continues next session.
  * The attached protective stop activates once the entry has any fill. If a
    session's low reaches it, it fills at min(open, stop) minus slippage
    (gaps through the stop fill at the worse open price).
  * The planned exit sells the remaining position at the planned exit
    session's close minus slippage, and cancels the stop.
  * Manual exits are market sells at the next open minus slippage.
  * No commissions. Slippage is the only modelled trading cost.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

from .base import BrokerResult, OrderRequest

SLIPPAGE_BPS = 5.0
PARTICIPATION = 0.02
NAME = "simulator"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slip(price: float, side: str) -> float:
    adj = SLIPPAGE_BPS / 10000
    return round(price * (1 + adj) if side == "buy" else price * (1 - adj), 4)


def insert_order(conn, *, client_order_id, symbol, side, qty, order_type, limit_price, stop_price,
                 purpose, parent_id=None, status="pending", reason=None, eligible_from=None) -> str:
    oid = "ord_" + uuid.uuid4().hex[:12]
    ts = now()
    conn.execute(
        """INSERT INTO orders(id,client_order_id,broker,broker_order_id,symbol,side,qty,order_type,limit_price,stop_price,
           purpose,parent_id,status,status_reason,filled_qty,avg_fill_price,created_at,updated_at,eligible_from)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,?,?,?)""",
        (oid, client_order_id, NAME, oid, symbol, side, qty, order_type, limit_price, stop_price,
         purpose, parent_id, status, reason, ts, ts, eligible_from))
    return oid


def submit(conn, req: OrderRequest, buying_power: float, ref_price: float, eligible_from: str | None = None) -> BrokerResult:
    """Accept or reject an entry. Returns the entry's state; the stop is stored as a held child order."""
    reason = None
    if req.qty <= 0:
        reason = "quantity must be positive"
    elif req.order_type == "limit" and not req.limit_price:
        reason = "limit order needs a limit price"
    elif req.stop_price is not None and req.side == "buy" and req.stop_price >= (req.limit_price or ref_price):
        reason = "stop must be below the entry price for a long position"
    elif req.qty * (req.limit_price or ref_price) > buying_power:
        reason = f"insufficient simulated buying power (${buying_power:,.0f})"
    status = "rejected" if reason else "pending"
    oid = insert_order(conn, client_order_id=req.client_order_id, symbol=req.symbol, side=req.side, qty=req.qty,
                       order_type=req.order_type, limit_price=req.limit_price, stop_price=None, purpose="entry",
                       status=status, reason=reason or f"accepted; fills at the {eligible_from or 'next'} session open",
                       eligible_from=eligible_from)
    if not reason and req.stop_price is not None:
        insert_order(conn, client_order_id=req.client_order_id + ":stop", symbol=req.symbol, side="sell", qty=req.qty,
                     order_type="stop", limit_price=None, stop_price=req.stop_price, purpose="stop", parent_id=oid,
                     reason="held until entry fills")
    return BrokerResult(oid, status, reason)


def _fill(conn, order, session, qty, price, note):
    conn.execute("INSERT INTO fills(order_id,session_date,qty,price,note) VALUES(?,?,?,?,?)",
                 (order["id"], session, qty, price, note))
    new_filled = order["filled_qty"] + qty
    prev_cost = (order["avg_fill_price"] or 0) * order["filled_qty"]
    avg = round((prev_cost + qty * price) / new_filled, 4)
    status = "filled" if new_filled >= order["qty"] else "partially_filled"
    conn.execute("UPDATE orders SET filled_qty=?, avg_fill_price=?, status=?, status_reason=?, updated_at=? WHERE id=?",
                 (new_filled, avg, status, note, now(), order["id"]))


def _set_status(conn, oid, status, reason):
    conn.execute("UPDATE orders SET status=?, status_reason=?, updated_at=? WHERE id=?", (status, reason, now(), oid))


def _open_qty(conn, entry_id) -> int:
    bought = conn.execute("SELECT COALESCE(SUM(qty),0) FROM fills WHERE order_id=?", (entry_id,)).fetchone()[0]
    sold = conn.execute("""SELECT COALESCE(SUM(f.qty),0) FROM fills f JOIN orders o ON o.id=f.order_id
                           WHERE o.parent_id=? AND o.side='sell'""", (entry_id,)).fetchone()[0]
    return bought - sold


def _close_children(conn, entry_id, reason):
    for o in conn.execute("SELECT id FROM orders WHERE parent_id=? AND status IN ('pending','partially_filled')", (entry_id,)):
        _set_status(conn, o["id"], "canceled", reason)
    e = conn.execute("SELECT * FROM orders WHERE id=?", (entry_id,)).fetchone()
    if e["status"] in ("pending", "partially_filled"):
        _set_status(conn, entry_id, "canceled", f"remainder canceled: {reason}")


def usable(b: dict | None) -> bool:
    """A bar the simulator can trade on. Provider gaps (None prices) are skipped, never treated as zero."""
    return bool(b) and None not in (b.get("open"), b.get("low"), b.get("close"))


def _eligible(o, session: str) -> bool:
    """Orders placed while a session was already trading may not fill at that session's (past) open."""
    ef = o["eligible_from"] if "eligible_from" in o.keys() else None
    return ef is None or session >= ef


def process_session(conn, session: str, bars: dict[str, dict], plan_exits: dict[str, str]) -> list[str]:
    """Run one simulated session. `plan_exits` maps simulator entry order id -> planned exit date."""
    events = []
    q = lambda sql, *a: conn.execute(sql, a).fetchall()  # noqa: E731

    def plan_exit(entry_id, at, price_field, note):
        e = conn.execute("SELECT * FROM orders WHERE id=?", (entry_id,)).fetchone()
        qty = _open_qty(conn, entry_id)
        b = bars.get(e["symbol"])
        if qty <= 0:
            if e["status"] in ("pending", "partially_filled"):
                _close_children(conn, entry_id, "planned exit date reached")
            return
        if not usable(b):
            events.append(f"{e['symbol']}: planned exit due but no usable bar on {session}; will exit at the next usable open")
            return
        n = conn.execute("SELECT COUNT(*) FROM orders WHERE parent_id=? AND purpose='plan_exit'", (entry_id,)).fetchone()[0]
        oid = insert_order(conn, client_order_id=f"{e['client_order_id']}:planexit:{n + 1}", symbol=e["symbol"], side="sell", qty=qty,
                           order_type="market_on_close" if price_field == "close" else "market", limit_price=None, stop_price=None,
                           purpose="plan_exit", parent_id=entry_id, reason=note)
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        price = _slip(b[price_field], "sell")
        _fill(conn, o, session, qty, price, f"{note}: {qty} @ {price:.2f} at {session} {at}")
        _close_children(conn, entry_id, "planned exit filled")
        events.append(f"{e['symbol']}: {note} filled {qty} @ {price:.2f}")

    # 0) Open: overdue planned exits (the exit day's bar was missing). Exit at the first usable open after it.
    for entry_id, exit_date in plan_exits.items():
        if exit_date < session:
            plan_exit(entry_id, "open", "open", "late planned exit (exit-day bar was missing)")

    # 1) Open: entries and manual exits.
    for o in q("SELECT * FROM orders WHERE broker=? AND purpose='entry' AND status IN ('pending','partially_filled') AND COALESCE(style,'swing')='swing' ORDER BY created_at", NAME):
        b = bars.get(o["symbol"])
        if not _eligible(o, session):
            continue
        if not usable(b):
            events.append(f"{o['symbol']}: no usable bar on {session} (provider gap); order keeps working")
            continue
        if not b.get("volume"):
            events.append(f"{o['symbol']}: no reported volume on {session}; order keeps working")
            continue
        remaining = o["qty"] - o["filled_qty"]
        cap = max(1, math.floor(PARTICIPATION * b["volume"]))
        take = min(remaining, cap)
        if o["order_type"] == "market":
            price = _slip(b["open"], "buy")
        elif b["low"] <= o["limit_price"]:
            price = min(b["open"], o["limit_price"])
        else:
            events.append(f"{o['symbol']}: limit {o['limit_price']} not reached (low {b['low']})")
            continue
        note = f"filled {take} @ {price:.2f} at {session} open" + (f" (capped at {PARTICIPATION:.0%} of volume)" if take < remaining else "")
        _fill(conn, o, session, take, price, note)
        events.append(f"{o['symbol']}: entry {note}")
        conn.execute("UPDATE orders SET status_reason='active: protecting filled shares', updated_at=? WHERE parent_id=? AND purpose='stop' AND status='pending'",
                     (now(), o["id"]))

    for o in q("SELECT * FROM orders WHERE broker=? AND purpose='manual_exit' AND status='pending' AND COALESCE(style,'swing')='swing'", NAME):
        b = bars.get(o["symbol"])
        if not _eligible(o, session) or not usable(b):
            continue
        qty = _open_qty(conn, o["parent_id"])
        if qty <= 0:
            _set_status(conn, o["id"], "canceled", "no open position")
            continue
        conn.execute("UPDATE orders SET qty=? WHERE id=?", (qty, o["id"]))
        o = conn.execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
        price = _slip(b["open"], "sell")
        _fill(conn, o, session, qty, price, f"manual exit {qty} @ {price:.2f} at {session} open")
        _close_children(conn, o["parent_id"], "position closed manually")
        events.append(f"{o['symbol']}: manual exit filled {qty} @ {price:.2f}")

    # 2) Intraday: protective stops on filled shares.
    for st in q("SELECT * FROM orders WHERE broker=? AND purpose='stop' AND status='pending' AND COALESCE(style,'swing')='swing'", NAME):
        qty = _open_qty(conn, st["parent_id"])
        b = bars.get(st["symbol"])
        if qty <= 0 or not usable(b):
            continue
        if b["low"] <= st["stop_price"]:
            conn.execute("UPDATE orders SET qty=? WHERE id=?", (qty, st["id"]))
            st = conn.execute("SELECT * FROM orders WHERE id=?", (st["id"],)).fetchone()
            price = _slip(min(b["open"], st["stop_price"]), "sell")
            _fill(conn, st, session, qty, price, f"stop hit: sold {qty} @ {price:.2f} (low {b['low']})")
            _close_children(conn, st["parent_id"], "stop filled")
            events.append(f"{st['symbol']}: stop filled {qty} @ {price:.2f}")

    # 3) Close: planned time exits due today.
    for entry_id, exit_date in plan_exits.items():
        if exit_date == session:
            plan_exit(entry_id, "close", "close", "planned exit")
    return events
