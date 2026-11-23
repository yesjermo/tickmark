"""Service layer for day trading: session control, screening, previews, orders, flattening."""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from . import alerts, db, events, intraday, risk
from .brokers import simulator as sim
from .tradingcal import is_session


class DayError(Exception):
    def __init__(self, msg, code=400, payload=None):
        super().__init__(msg)
        self.code, self.payload = code, payload


def _watchlist(conn):
    return db.kv_get(conn, "watchlist", [])


def _require_live_data():
    if db.data_mode() != "live":
        raise DayError("Day trading uses real intraday prices. Switch to Live data first.", 409)


def state() -> dict:
    c = db.connect()
    try:
        mode, day, cur = intraday.current(c)
        p, v = intraday.active_params(c)
        hist = [{"version": r["version"], "params": json.loads(r["params_json"]), "created_at": r["created_at"]}
                for r in c.execute("SELECT * FROM day_rulesets ORDER BY version DESC LIMIT 20")]
        _, _, market_open = intraday.live_cursor()
        return {"mode": mode, "date": day, "cursor": cur, "market_open": market_open, "feed": intraday.feed_for(mode),
                "params": asdict(p), "version": v, "history": hist, "strategies": intraday.DAY_STRATEGIES,
                "data_mode": db.data_mode()}
    finally:
        c.close()


def set_session(mode: str, day: str | None = None) -> dict:
    _require_live_data()
    if mode not in ("live", "replay"):
        raise DayError("mode must be live or replay")
    if mode == "replay":
        d = date.fromisoformat(day or "")
        if not is_session(d) or d >= intraday.now_et().date() and intraday.live_cursor()[2]:
            raise DayError("Pick a past trading session to replay.")
        if d >= intraday.now_et().date():
            raise DayError("Replay needs a completed past session.")
        c = db.connect()
        try:
            syms = _watchlist(c)
            p, _ = intraday.active_params(c)
        finally:
            c.close()
        info = intraday.ensure_data(syms, d, intraday.feed_for("replay"), p.rvol_days)
        first_end = intraday.end_of(f"{d.isoformat()}T09:30")
        with db.tx() as conn:
            db.kv_set(conn, "day_session", {"mode": "replay", "date": d.isoformat(), "cursor": first_end})
            db.kv_set(conn, f"day_processed:{d.isoformat()}", f"{d.isoformat()}T09:30")
        events.bump("day session")
        return {**state(), "fetched": info}
    with db.tx() as conn:
        db.kv_set(conn, "day_session", {"mode": "live"})
    events.bump("day session")
    return state()


def step(n: int = 1) -> dict:
    """Replay: advance the cursor n bars and run the intraday simulator over them."""
    with db.tx() as conn:
        s = intraday.session(conn)
        if s.get("mode") != "replay":
            raise DayError("Stepping is only for replay sessions; live sessions follow the clock.", 409)
        cur = datetime.fromisoformat(s["cursor"])
        new = min(cur + timedelta(minutes=intraday.BAR_MIN * max(1, n)), datetime.fromisoformat(f"{s['date']}T16:00"))
        new_cur = new.strftime("%Y-%m-%dT%H:%M")
        ev = _advance(conn, s["date"], new_cur)
        db.kv_set(conn, "day_session", {**s, "cursor": new_cur})
    alerts.flush()
    events.bump("day step")
    return {"cursor": new_cur, "events": ev}


def _advance(conn, day: str, upto: str) -> list[str]:
    from .trading import refresh_outcomes
    ev = intraday.process_manual(conn, day, upto, "")
    ev += intraday.process(conn, day, upto)
    refresh_outcomes(conn)
    return ev


def sync_live() -> dict:
    """Live: refresh today's bars (cached ~1 min) and run the simulator on newly completed bars."""
    c = db.connect()
    try:
        mode, day, cur = intraday.current(c)
        syms = _watchlist(c)
        p, _ = intraday.active_params(c)
        last = db.kv_get(c, "day_live_fetch_at")
    finally:
        c.close()
    if mode != "live":
        return {"skipped": "replay"}
    if not is_session(date.fromisoformat(day)):
        return {"skipped": "no session today"}
    now = datetime.now(timezone.utc).timestamp()
    info = {}
    if not last or now - last > 55:
        info = intraday.ensure_data(syms, date.fromisoformat(day), intraday.feed_for("live"), p.rvol_days, refresh_today=True)
    with db.tx() as conn:
        db.kv_set(conn, "day_live_fetch_at", now)
        ev = _advance(conn, day, cur)
    if ev:
        alerts.flush()
        events.bump("day live")
    return {"fetched": info, "events": ev}


def screen() -> dict:
    _require_live_data()
    if intraday.current(db.connect())[0] == "live":
        try:
            sync_live()
        except Exception as exc:
            live_err = str(exc)[:200]
        else:
            live_err = None
    else:
        live_err = None
    c = db.connect()
    try:
        mode, day, cur = intraday.current(c)
        p, v = intraday.active_params(c)
        out = []
        for sym in _watchlist(c):
            today = intraday.bars_until(c, sym, day, cur)
            priors = [intraday.day_bars(c, sym, d.isoformat()) for d in intraday.prior_sessions(date.fromisoformat(day), p.rvol_days)]
            priors = [x for x in priors if x]
            quote = intraday.latest_quote(sym) if mode == "live" else None
            out.append(intraday.evaluate(sym, today, priors, p, quote, mode, cur, day))
        return {"mode": mode, "date": day, "cursor": cur, "version": v, "results": out, "error": live_err}
    finally:
        c.close()


def chart(symbol: str) -> dict:
    c = db.connect()
    try:
        mode, day, cur = intraday.current(c)
        p, _ = intraday.active_params(c)
        bars = intraday.bars_until(c, symbol, day, cur)
        vw = intraday.vwap(bars)
        hi, lo, _ = intraday.opening_range(bars, p.or_minutes)
        fills = [dict(r) for r in c.execute("""SELECT f.*, o.side, o.purpose FROM fills f JOIN orders o ON o.id=f.order_id
                                               WHERE o.symbol=? AND o.style='intraday' AND f.session_date=?""", (symbol, day))]
        return {"symbol": symbol, "date": day, "cursor": cur, "mode": mode,
                "bars": [{"ts": b["ts"], "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"], "volume": b["volume"]} for b in bars],
                "vwap": [round(x, 4) if x else None for x in vw], "opening_range": None if hi is None else {"high": hi, "low": lo},
                "fills": fills, "source": bars[-1]["source"] if bars else None}
    finally:
        c.close()


def route(conn, mode: str) -> dict:
    """Where a day trade would go. Broker routing needs: an external broker with bracket orders, the live
    session (replays are always simulated), an open market, and the paper environment."""
    from .brokers import factory
    from .trading import broker_mode
    bm = broker_mode(conn)
    if not factory.is_external(bm):
        return {"broker": "simulator", "label": factory.LABELS["simulator"], "why": "Broker mode is the local simulator, so the intraday simulator fills it. Switch the Broker tab to Alpaca to send live-session day trades there."}
    label = factory.LABELS[bm]
    if mode != "live":
        return {"broker": "simulator", "label": factory.LABELS["simulator"], "why": f"Replays are always simulated, even with {label} connected."}
    if not factory.capabilities(bm).get("bracket"):
        return {"broker": "simulator", "label": factory.LABELS["simulator"],
                "why": f"{label} day-trade brackets aren't supported yet; simulated instead."}
    if db.environment() != "paper":
        return {"broker": None, "label": label, "why": "Day trades with real money are not enabled. Switch back to paper.", "blocked": True}
    if not intraday.live_cursor()[2]:
        return {"broker": None, "label": label, "why": "The market is closed.", "blocked": True}
    return {"broker": bm, "label": label,
            "why": f"Sent to {label} paper as a bracket (entry, take-profit and stop; DAY orders). Tickmark sells anything still open at the flat-by time."}


def preview(symbol: str, qty_override: int | None = None) -> dict:
    from .trading import broker_account, sizing_account
    acct = broker_account()
    sc = screen()
    ev = next((r for r in sc["results"] if r["symbol"] == symbol), None)
    if not ev:
        raise DayError(f"{symbol} isn't on the watchlist", 404)
    if ev["status"] != "QUALIFIES":
        raise DayError(f"{symbol} doesn't qualify right now ({ev['status'].replace('_', ' ').lower()}); no day-trade preview.", 409)
    with db.tx() as conn:
        if sc["version"] == 0:
            raise DayError("Save your day-trading rules first; previews record the rule version.", 409)
        p, v = intraday.active_params(conn)
        equity, bp = sizing_account(conn, acct)
        entry, stop, target = ev["entry_assumption"], ev["suggested_stop"], ev["target"]
        per = entry - stop
        budget = equity * p.risk_per_trade_pct / 100
        sized = max(0, min(math.floor(budget / per), math.floor(equity * p.max_position_pct / 100 / entry)))
        qty = int(qty_override or sized)
        rt = route(conn, sc["mode"])
        rk = risk.evaluate(conn, symbol=symbol, qty=qty, entry=entry, stop=stop, equity=equity, buying_power=bp,
                           broker=rt["broker"] or "simulator", broker_account=acct, session=sc["date"], style="intraday")
        pid = "dv_" + uuid.uuid4().hex[:16]
        pv = {"id": pid, "style": "intraday", "symbol": symbol, "side": "buy", "qty": qty, "suggested_qty": sized,
              "order_type": "market", "limit_price": None, "entry_assumption": entry, "stop_price": stop, "suggested_stop": stop,
              "target": target, "est_position_value": round(qty * entry, 2), "est_loss_if_stopped": round(qty * per, 2),
              "risk_budget": round(budget, 2), "equity": equity, "plan": ev["plan"], "flat_by": p.flat_by,
              "cursor": sc["cursor"], "date": sc["date"], "mode": sc["mode"],
              "route": rt, "ruleset_version": v, "data_timestamp": ev.get("data_timestamp"), "earnings_warning": None, "evaluation": ev, "risk": rk,
              "fills_at": "next 5-minute bar open" if rt["broker"] == "simulator" else f"market, at {rt['label']}",
              "fill_disclaimer": ("Intraday fills can differ a lot from the last price: fast markets gap through stops, and a "
                                  "position still open at the flat-by time is sold at that bar's close."),
              "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")}
        conn.execute("INSERT INTO previews(id,payload_json,created_at) VALUES(?,?,?)", (pid, json.dumps(pv), datetime.now(timezone.utc).isoformat()))
    return pv


def submit(preview_id: str, confirm: bool, thesis: str, risk_overrides: dict | None) -> dict:
    from .trading import _write_journal, order_view, broker_account
    if not confirm:
        raise DayError("Submission requires explicit confirmation.")
    acct = broker_account()          # network read before taking the write lock
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM orders WHERE client_order_id=?", (preview_id,)).fetchone():
            raise DayError("Duplicate submission blocked: this preview was already submitted.", 409, {"duplicate": True})
        row = conn.execute("SELECT payload_json FROM previews WHERE id=?", (preview_id,)).fetchone()
        if not row:
            raise DayError("Unknown preview.", 404)
        pv = json.loads(row["payload_json"])
        mode, day, cur = intraday.current(conn)
        if datetime.fromisoformat(pv["expires_at"]) < datetime.now(timezone.utc) or cur != pv["cursor"]:
            raise DayError("A new bar has completed since this preview; open a fresh one.", 409)
        p, v = intraday.active_params(conn)
        if v != pv["ruleset_version"]:
            raise DayError("Day-trading rules changed since this preview; open a fresh one.", 409)
        open_same = conn.execute("""SELECT id FROM orders WHERE symbol=? AND style='intraday' AND purpose='entry'
                                    AND (status IN ('pending','partially_filled'))""", (pv["symbol"],)).fetchone()
        if open_same or any(sim._open_qty(conn, r["id"]) > 0 for r in conn.execute(
                "SELECT id FROM orders WHERE symbol=? AND style='intraday' AND purpose='entry'", (pv["symbol"],))):
            raise DayError(f"You already have a working day-trade order or position in {pv['symbol']}.", 409)
        from .trading import account
        rt = route(conn, mode)
        if rt.get("blocked"):
            raise DayError(rt["why"], 409)
        if rt["broker"] != (pv.get("route") or {}).get("broker", "simulator"):
            raise DayError(f"Order routing changed since this preview (now: {rt['label']}); open a fresh one.", 409)
        external = rt["broker"] != "simulator"
        equity = pv["equity"]
        bp = acct["buying_power"] if (external and acct) else account(conn)["buying_power"]
        rk = risk.evaluate(conn, symbol=pv["symbol"], qty=pv["qty"], entry=pv["entry_assumption"], stop=pv["stop_price"],
                           equity=equity, buying_power=bp, broker=rt["broker"], broker_account=acct if external else None,
                           session=day, style="intraday")
        used = risk.enforce(rk, risk_overrides)
        risk.log_overrides(conn, preview_id, pv["symbol"], used)
        rs = {"version": v, "name": f"{intraday.DAY_STRATEGIES[p.strategy]['name']} (day) v{v}", "params": asdict(p), "style": "intraday"}
        auto = (f"{pv['symbol']} {pv['date']} {pv['cursor'][11:]}: {rs['name']} signal. "
                f"Stop {pv['stop_price']}, target {pv['target']}, flat by {p.flat_by}.")
        if external:
            # Reserve the entry row (UNIQUE client_order_id blocks duplicates) and journal it, then call the broker
            # outside the transaction. The stop and take-profit rows appear when sync sees the bracket legs.
            oid = sim.insert_order(conn, client_order_id=preview_id, symbol=pv["symbol"], side="buy", qty=pv["qty"], order_type="market",
                                   limit_price=None, stop_price=pv["stop_price"], purpose="entry", status="pending",
                                   reason=f"sending to {rt['label']} as a bracket")
            conn.execute("UPDATE orders SET broker=?, broker_order_id=NULL, style='intraday', target_price=? WHERE id=?",
                         (rt["broker"], pv["target"], oid))
            pv2 = {**pv, "mode": rt["broker"], "_ruleset": rs, "_risk": rk, "_overrides": used,
                   "plan": {**pv["plan"], "flat_by": p.flat_by, "target": pv["target"], "style": "intraday"}}
            _write_journal(conn, oid, pv2, thesis or auto, False)
        if not external:
            nxt = [b for b in intraday.day_bars(conn, pv["symbol"], day) if b["ts"] >= cur]
            eligible = nxt[0]["ts"] if nxt else intraday._iso(datetime.fromisoformat(cur))
            oid = sim.insert_order(conn, client_order_id=preview_id, symbol=pv["symbol"], side="buy", qty=pv["qty"], order_type="market",
                                   limit_price=None, stop_price=pv["stop_price"], purpose="entry",
                                   reason=f"day trade; fills at the {eligible[11:]} bar open", eligible_from=eligible)
            conn.execute("UPDATE orders SET style='intraday', target_price=? WHERE id=?", (pv["target"], oid))
            for purpose, px, typ in (("stop", pv["stop_price"], "stop"), ("target", pv["target"], "limit")):
                cid = sim.insert_order(conn, client_order_id=f"{preview_id}:{purpose}", symbol=pv["symbol"], side="sell", qty=pv["qty"],
                                       order_type=typ, limit_price=px if purpose == "target" else None,
                                       stop_price=px if purpose == "stop" else None, purpose=purpose, parent_id=oid,
                                       reason="OCO with the other exit; active once the entry fills")
                conn.execute("UPDATE orders SET style='intraday' WHERE id=?", (cid,))
            pv2 = {**pv, "mode": "simulator", "_ruleset": rs, "_risk": rk, "_overrides": used, "fills_at_open_of": eligible,
                   "plan": {**pv["plan"], "flat_by": p.flat_by, "target": pv["target"], "style": "intraday"}}
            _write_journal(conn, oid, pv2, thesis or auto, False)
            return {"order": order_view(conn, oid)}
    return _send_bracket(oid, rt, pv)


def _send_bracket(oid: str, rt: dict, pv: dict) -> dict:
    from . import broker_sync
    from .brokers import factory
    from .brokers.base import BrokerError, OrderRequest
    from .trading import order_view, refresh_outcomes
    req = OrderRequest(pv["id"], pv["symbol"], "buy", pv["qty"], "market", None, pv["stop_price"])
    try:
        res = factory.adapter(rt["broker"]).submit_bracket(req, pv["target"])
    except BrokerError as exc:
        res = None
        err = f"broker error: {exc}"
    with db.tx() as conn:
        row = dict(conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone())
        if res is None:
            sim._set_status(conn, oid, "rejected", err)
        else:
            conn.execute("UPDATE orders SET broker_order_id=? WHERE id=?", (res.broker_order_id, oid))
            row["broker_order_id"] = res.broker_order_id
            broker_sync.apply_result(conn, row, res, intraday.now_et().date().isoformat())
        refresh_outcomes(conn)
        return {"order": order_view(conn, oid)}


def flatten(order_id: str) -> dict:
    c = db.connect()
    try:
        e = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    finally:
        c.close()
    if e and e["broker"] != "simulator":
        from . import broker_sync
        from .trading import TradeError
        if e["style"] != "intraday":
            raise DayError("not a day-trade order", 409)
        try:
            return {"order": broker_sync.flatten_day_trade(order_id, "manual_exit", "manual flatten (market)")}
        except TradeError as exc:
            raise DayError(str(exc), exc.code)
    with db.tx() as conn:
        mode, day, cur = intraday.current(conn)
        try:
            return intraday.manual_flatten(conn, order_id, day, cur)
        except ValueError as exc:
            raise DayError(str(exc), 409)


def positions() -> list[dict]:
    c = db.connect()
    try:
        mode, day, cur = intraday.current(c)
        out = []
        for o in c.execute("SELECT * FROM orders WHERE style='intraday' AND purpose='entry' ORDER BY created_at DESC LIMIT 20").fetchall():
            held = sim._open_qty(c, o["id"])
            bars = intraday.bars_until(c, o["symbol"], day, cur)
            last = bars[-1]["close"] if bars else None
            j = c.execute("SELECT jo.* FROM journal j JOIN journal_outcomes jo ON jo.journal_id=j.id WHERE j.order_id=?", (o["id"],)).fetchone()
            out.append({"id": o["id"], "symbol": o["symbol"], "status": o["status"], "qty": o["qty"], "held": held, "broker": o["broker"],
                        "entry": o["avg_fill_price"], "stop": o["stop_price"], "target": o["target_price"], "last": last,
                        "open_pnl": None if not (held and last and o["avg_fill_price"]) else round((last - o["avg_fill_price"]) * held, 2),
                        "closed": dict(j) if j and j["status"] == "closed" else None, "created_at": o["created_at"]})
        return out
    finally:
        c.close()
