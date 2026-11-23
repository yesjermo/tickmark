"""Broker-side order lifecycle and reconciliation for Alpaca Paper.

The broker is the source of truth for broker orders. This module:
  * syncs order status and fills (including the OTO stop leg) into the local
    orders/fills tables, so the journal computes P&L and plan adherence from
    the broker's own fills, exactly as it does for the simulator;
  * runs planned exits: on the exit date it cancels the stop leg and sends a
    market-on-close sell (before 15:50 ET); an overdue exit is sent as a
    market sell at the next opportunity;
  * handles manual "close now" the same way with a market sell;
  * reconciles broker positions and open orders against the journal and flags
    breaks (untracked positions, quantity mismatches, positions without a stop).

Network calls never run inside a database transaction.
"""
from __future__ import annotations

import json
import time as _time
from datetime import datetime, timezone

from . import db, live_data
from .brokers import factory
from .brokers import simulator as sim
from .brokers.base import OPEN_STATUSES, BrokerError, BrokerResult
MOC_CUTOFF = (15, 50)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(sql, *args):
    c = db.connect()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


# ---------- applying broker state locally ----------

def _apply(conn, row: dict, res_status: str, reason: str | None, filled: int, avg: float | None, session: str) -> None:
    """Update one local order from broker state; record any new fill quantity as a fill row."""
    old_q, old_avg = row["filled_qty"] or 0, row["avg_fill_price"] or 0.0
    if filled > old_q and avg is not None:
        delta = filled - old_q
        px = avg if old_q == 0 else (avg * filled - old_avg * old_q) / delta
        conn.execute("INSERT INTO fills(order_id,session_date,qty,price,note) VALUES(?,?,?,?,?)",
                     (row["id"], session, delta, round(px, 4), f"broker fill {delta} @ {px:.2f} ({row['broker']})"))
    conn.execute("UPDATE orders SET status=?, status_reason=?, filled_qty=?, avg_fill_price=?, updated_at=? WHERE id=?",
                 (res_status, reason, max(filled, old_q), avg if avg is not None else row["avg_fill_price"], sim.now(), row["id"]))


LEG_PURPOSE = {"stop": "stop", "stop_limit": "stop", "limit": "target"}


def _ensure_leg(conn, entry: dict, leg: dict, purpose: str) -> dict:
    """Local row for a broker child leg (stop or take-profit), inheriting the entry's broker and style."""
    r = conn.execute("SELECT * FROM orders WHERE parent_id=? AND purpose=?", (entry["id"], purpose)).fetchone()
    if r:
        if not r["broker_order_id"]:
            conn.execute("UPDATE orders SET broker_order_id=? WHERE id=?", (leg["id"], r["id"]))
        return dict(conn.execute("SELECT * FROM orders WHERE id=?", (r["id"],)).fetchone())
    oid = sim.insert_order(conn, client_order_id=f"{entry['client_order_id']}:{purpose}", symbol=entry["symbol"], side="sell",
                           qty=entry["qty"], order_type=leg["type"], limit_price=leg.get("limit_price"), stop_price=leg.get("stop_price"),
                           purpose=purpose, parent_id=entry["id"], reason=f"broker {purpose} leg ({leg['raw_status']})")
    conn.execute("UPDATE orders SET broker=?, broker_order_id=?, style=? WHERE id=?",
                 (entry["broker"], leg["id"], entry.get("style") or "swing", oid))
    return dict(conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone())


def _ensure_stop_leg(conn, entry: dict, leg: dict) -> dict:
    return _ensure_leg(conn, entry, leg, "stop")


def sync(session: str | None = None) -> dict:
    """Pull status for every working broker order (and the stop legs of open positions)."""
    rows = _read(f"""SELECT * FROM orders WHERE broker!='simulator' AND broker_order_id IS NOT NULL AND
                     (status IN ('pending','partially_filled') OR (purpose='entry' AND id IN
                       (SELECT j.order_id FROM journal j JOIN journal_outcomes o ON o.journal_id=j.id WHERE o.status='open')))""")
    if not rows:
        return {"synced": 0, "errors": []}
    adapters, fetched, errors = {}, [], []
    for r in rows:
        try:
            b = adapters.get(r["broker"]) or adapters.setdefault(r["broker"], factory.adapter(r["broker"]))
            fetched.append((r, b.get(r["broker_order_id"])))
        except Exception as exc:
            errors.append(f"{r['symbol']} {r['purpose']}: {type(exc).__name__}: {exc}"[:160])
    session = session or live_data.now_et().date().isoformat()
    with db.tx() as conn:
        for r, res in fetched:
            cur = dict(conn.execute("SELECT * FROM orders WHERE id=?", (r["id"],)).fetchone())
            apply_result(conn, cur, res, session)
        from .trading import refresh_outcomes
        refresh_outcomes(conn)
        db.kv_set(conn, "broker_sync", {"at": _now(), "synced": len(fetched), "errors": errors})
    return {"synced": len(fetched), "errors": errors}


def apply_result(conn, row: dict, res: BrokerResult, session: str) -> None:
    """Apply one broker order state (and, for entries, its stop leg) to the local order rows."""
    _apply(conn, row, res.status, res.status_reason, res.filled_qty, res.avg_fill_price, session)
    if row["purpose"] == "entry":
        for leg in res.legs:
            purpose = LEG_PURPOSE.get(leg["type"])
            if leg["side"] == "sell" and purpose:
                child = _ensure_leg(conn, row, leg, purpose)
                _apply(conn, child, leg["status"], f"broker {purpose} leg ({leg['raw_status']})",
                       leg["filled_qty"], leg["avg_fill_price"], session)


def apply_event(broker_id: str, res: BrokerResult, client_order_id: str | None, session: str) -> str:
    """Apply a pushed order update. Returns 'applied', or 'resynced' when the order isn't known locally
    (e.g. a stop leg seen before its row exists), in which case a full sync fills the gap."""
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM orders WHERE broker=? AND broker_order_id=?", (broker_id, res.broker_order_id)).fetchone()
        if not row and client_order_id:
            row = conn.execute("SELECT * FROM orders WHERE broker=? AND client_order_id=?", (broker_id, client_order_id)).fetchone()
            if row and not row["broker_order_id"]:
                conn.execute("UPDATE orders SET broker_order_id=? WHERE id=?", (res.broker_order_id, row["id"]))
        if row:
            apply_result(conn, dict(conn.execute("SELECT * FROM orders WHERE id=?", (row["id"],)).fetchone()), res, session)
            from .trading import refresh_outcomes
            refresh_outcomes(conn)
            return "applied"
    sync(session)
    return "resynced"


# ---------- exits ----------

def _cancel_and_wait(b, rows) -> None:
    """Cancel open child legs (stop, take-profit) first: while they are open the broker holds the shares
    and would reject the sell."""
    rows = [r for r in (rows if isinstance(rows, list) else [rows])
            if r and r["broker_order_id"] and r["status"] in OPEN_STATUSES]
    for r in rows:
        b.cancel(r["broker_order_id"])
    for r in rows:
        for _ in range(10):
            if b.get(r["broker_order_id"]).status not in OPEN_STATUSES:
                break
            _time.sleep(0.5)
        else:
            raise BrokerError(f"{r['purpose']} leg did not cancel in time; exit not sent (position still protected)")


def _send_exit(entry: dict, purpose: str, at_close: bool, note: str) -> dict:
    from .trading import TradeError
    legs = _read("SELECT * FROM orders WHERE parent_id=? AND purpose IN ('stop','target')", entry["id"])
    existing = _read("SELECT * FROM orders WHERE parent_id=? AND purpose IN ('plan_exit','manual_exit','flat') AND status IN ('pending','partially_filled')",
                     entry["id"])
    if existing:
        raise TradeError("An exit order is already working at the broker.", 409)
    qty = _open_qty(entry["id"])
    if qty <= 0:
        raise TradeError("No open broker position for this order.", 409)
    n = len(_read("SELECT id FROM orders WHERE parent_id=? AND purpose=?", entry["id"], purpose))
    cid = f"{entry['client_order_id']}:{ {'plan_exit': 'planexit', 'flat': 'flat'}.get(purpose, 'manual') }:{n + 1}"
    b = factory.adapter(entry["broker"])
    at_close = at_close and factory.capabilities(entry["broker"])["market_on_close"]
    if not at_close and purpose == "plan_exit" and "close" in note:
        note = note.replace("at the", "near the") + " (market order; this broker has no market-on-close)"
    try:
        if entry["status"] in OPEN_STATUSES:       # stop buying the remainder before exiting
            b.cancel(entry["broker_order_id"])
        _cancel_and_wait(b, legs)
        res = b.sell(entry["symbol"], qty, cid, at_close=at_close)
    except BrokerError as exc:
        raise TradeError(f"{factory.LABELS.get(entry['broker'], 'Broker')}: {exc}", 502)
    with db.tx() as conn:
        oid = sim.insert_order(conn, client_order_id=cid, symbol=entry["symbol"], side="sell", qty=qty,
                               order_type="market_on_close" if at_close else "market", limit_price=None, stop_price=None,
                               purpose=purpose, parent_id=entry["id"], status=res.status, reason=f"{note} ({res.status_reason})")
        conn.execute("UPDATE orders SET broker=?, broker_order_id=?, style=? WHERE id=?",
                     (entry["broker"], res.broker_order_id, entry.get("style") or "swing", oid))
        for leg in legs:
            if leg["status"] in OPEN_STATUSES:
                sim._set_status(conn, leg["id"], "canceled", f"canceled before {purpose.replace('_', ' ')}")
        if entry["status"] in OPEN_STATUSES:
            sim._set_status(conn, entry["id"], "canceled", f"remainder canceled before {purpose.replace('_', ' ')}")
        from .trading import order_view
        return order_view(conn, oid)


def _open_qty(entry_id: str) -> int:
    c = db.connect()
    try:
        return sim._open_qty(c, entry_id)
    finally:
        c.close()


def manual_close(entry_id: str) -> dict:
    sync()
    entry = _read("SELECT * FROM orders WHERE id=?", entry_id)[0]
    return _send_exit(entry, "manual_exit", at_close=False, note="manual exit (market)")


def flatten_day_trade(entry_id: str, purpose: str = "flat", note: str = "flat by end of day (market)") -> dict:
    """Close a broker-held day trade: cancel its bracket legs, then sell at market."""
    sync()
    entry = _read("SELECT * FROM orders WHERE id=?", entry_id)[0]
    return _send_exit(entry, purpose, at_close=False, note=note)


def run_planned_exits(now: datetime | None = None) -> list[str]:
    """Send exits for broker positions whose planned exit date is today (MOC) or already passed (market)."""
    now = now or live_data.now_et()
    today = now.date().isoformat()
    before_cutoff = (now.hour, now.minute) < MOC_CUTOFF
    sync()
    events = []
    for j in _read(f"""SELECT j.order_id, j.plan_json FROM journal j JOIN journal_outcomes o ON o.journal_id=j.id
                      JOIN orders ord ON ord.id=j.order_id WHERE o.status='open' AND ord.broker!='simulator'
                      AND COALESCE(ord.style,'swing')='swing'"""):
        exit_date = json.loads(j["plan_json"])["exit_date"]
        if exit_date > today or (exit_date == today and not before_cutoff):
            continue   # not due yet, or today's MOC window has closed (it will go out as a late exit next run)
        entry = _read("SELECT * FROM orders WHERE id=?", j["order_id"])[0]
        if _open_qty(entry["id"]) <= 0:
            continue
        late = exit_date < today
        try:
            o = _send_exit(entry, "plan_exit", at_close=not late,
                           note="late planned exit (market)" if late else f"planned exit at the {today} close")
            events.append(f"{entry['symbol']}: {'late ' if late else ''}planned exit sent ({o['status']})")
        except Exception as exc:
            events.append(f"{entry['symbol']}: planned exit not sent: {exc}")
            from . import alerts
            with db.tx() as conn:
                alerts.emit(conn, "exit", f"Planned exit NOT sent: {entry['symbol']}",
                            f"{exc}. The position is still open at the broker; check the Broker tab.", "critical",
                            entry["symbol"], f"exitfail:{entry['id']}:{today}")
    return events


# ---------- reconciliation ----------

def reconcile(mode: str) -> dict:
    """Compare the journal's expected positions with the broker (or the simulator's own state)."""
    c = db.connect()
    try:
        from .trading import plan_exits
        entries = [dict(r) for r in c.execute(
            """SELECT ord.* FROM orders ord JOIN journal j ON j.order_id=ord.id JOIN journal_outcomes o ON o.journal_id=j.id
               WHERE o.status='open'""").fetchall()]
        expected: dict[str, int] = {}
        for e in entries:
            if e["broker"] == mode:
                expected[e["symbol"]] = expected.get(e["symbol"], 0) + sim._open_qty(c, e["id"])
        local_working = [dict(r) for r in c.execute(
            "SELECT * FROM orders WHERE status IN ('pending','partially_filled') AND broker=?",
            (mode,)).fetchall()]
        sim_exits = plan_exits(c)
    finally:
        c.close()
    breaks = []
    if factory.is_external(mode):
        b = factory.adapter(mode)
        try:
            actual = {s: p["qty"] for s, p in b.positions().items()}
            open_orders = b.open_orders()
        except BrokerError as exc:
            out = {"at": _now(), "mode": mode, "ok": False, "error": str(exc), "breaks": [], "checked": 0}
            _store(out)
            return out
        broker_ids, protected = set(), set()
        for o in open_orders:
            res = o["result"]
            broker_ids.add(res.broker_order_id)
            if o["side"] == "sell" and res.status in OPEN_STATUSES:
                protected.add(o["symbol"])          # standalone stop or exit order
            for leg in res.legs:
                broker_ids.add(leg["id"])
                if leg["side"] == "sell" and leg["status"] in OPEN_STATUSES:
                    protected.add(o["symbol"])      # OTO stop leg
        for sym in sorted(set(actual) | set(expected)):
            a, e = actual.get(sym, 0), expected.get(sym, 0)
            if a and not e:
                breaks.append({"type": "untracked_position", "symbol": sym, "detail": f"broker holds {a}, journal expects none (trade made outside Tickmark?)"})
            elif e and not a:
                breaks.append({"type": "missing_position", "symbol": sym, "detail": f"journal expects {e}, broker holds none"})
            elif a != e:
                breaks.append({"type": "qty_mismatch", "symbol": sym, "detail": f"broker {a} vs journal {e}"})
            if a > 0 and sym not in protected:
                breaks.append({"type": "unprotected_position", "symbol": sym, "detail": f"{a} shares with no open stop or exit order at the broker"})
        local_ids = {o["broker_order_id"] for o in local_working if o["broker_order_id"]}
        for o in local_working:
            if o["broker_order_id"] and o["broker_order_id"] not in broker_ids:
                breaks.append({"type": "stale_local_order", "symbol": o["symbol"],
                               "detail": f"{o['purpose']} {o['id']} is working locally but not open at the broker"})
        for o in open_orders:
            res = o["result"]
            ids = {res.broker_order_id, *(l["id"] for l in res.legs)}
            if not ids & local_ids and not _read("SELECT id FROM orders WHERE broker_order_id=?", res.broker_order_id):
                breaks.append({"type": "unknown_broker_order", "symbol": o["symbol"],
                               "detail": f"open broker order {res.broker_order_id} was not placed by Tickmark"})
        checked = len(set(actual) | set(expected))
    else:
        c = db.connect()
        try:
            for sym, e in expected.items():
                if e <= 0:
                    continue
                entry_ids = [x["id"] for x in entries if x["symbol"] == sym and x["broker"] == "simulator"]
                protected = c.execute(
                    f"SELECT 1 FROM orders WHERE parent_id IN ({','.join('?' * len(entry_ids))}) AND status='pending' "
                    "AND purpose IN ('stop','manual_exit')", entry_ids).fetchone()
                if not protected:
                    breaks.append({"type": "unprotected_position", "symbol": sym, "detail": f"{e} shares with no working stop"})
                if not any(eid in sim_exits for eid in entry_ids):
                    breaks.append({"type": "no_planned_exit", "symbol": sym, "detail": "open position has no planned exit"})
            for o in local_working:
                if o["parent_id"]:
                    p = c.execute("SELECT * FROM orders WHERE id=?", (o["parent_id"],)).fetchone()
                    if p and sim._open_qty(c, p["id"]) <= 0 and p["status"] not in OPEN_STATUSES:
                        breaks.append({"type": "orphan_order", "symbol": o["symbol"], "detail": f"{o['purpose']} {o['id']} works on a closed position"})
        finally:
            c.close()
        checked = len(expected)
    out = {"at": _now(), "mode": mode, "ok": not breaks, "breaks": breaks, "checked": checked}
    _store(out)
    return out


def _store(out: dict) -> None:
    from . import alerts
    with db.tx() as conn:
        db.kv_set(conn, "reconciliation", out)
        if out.get("breaks"):
            sig = ",".join(sorted(f"{b['type']}:{b['symbol']}" for b in out["breaks"]))
            alerts.emit(conn, "reconciliation", f"Reconciliation: {len(out['breaks'])} break(s)",
                        "\n".join(f"{b['symbol']}: {b['type'].replace('_', ' ')} - {b['detail']}" for b in out["breaks"]),
                        "critical", None, f"recon:{out['at'][:10]}:{sig}")
        elif out.get("error"):
            alerts.emit(conn, "reconciliation", "Reconciliation couldn't reach the broker", out["error"], "critical",
                        None, f"reconerr:{out['at'][:13]}")
