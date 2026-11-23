"""Service layer: screening, order previews, paper submission, the simulator clock and the journal."""
from __future__ import annotations

import json
import math
import threading
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from . import alerts, db, forward, live_data, risk
from .brokers import alpaca, factory
from .brokers import simulator as sim
from .brokers.base import OPEN_STATUSES, BrokerError, OrderRequest
from .rules import DEFAULT_TEXT, STRATEGIES, RuleParams, Series, evaluate_at, overlays, parse_strategy
from .tradingcal import is_session, next_sessions

MARKET_OPEN = (9, 30)

PREVIEW_TTL_MIN = 30


class TradeError(Exception):
    def __init__(self, msg, code=400, payload=None):
        super().__init__(msg)
        self.code, self.payload = code, payload


def utcnow():
    return datetime.now(timezone.utc)


# ---------- data ----------

def as_of(conn) -> str:
    return db.kv_get(conn, "as_of")


def first_fillable_session(conn) -> str:
    """The earliest session whose OPEN can fill an order placed now.

    Demo: the session after the simulator clock. Live: if a session is already
    trading (or done) today, its open is in the past, so the next session is the
    first fair fill. This prevents filling at an open the trader has already seen.
    """
    after = as_of(conn)
    nxt = next_sessions(date.fromisoformat(after), 1)[0] if after else None
    if db.data_mode() == "live":
        now = live_data.now_et()
        today = now.date()
        if is_session(today) and (now.hour, now.minute) < MARKET_OPEN:
            cand = today
        else:
            cand = next_sessions(today, 1)[0]
        if nxt is None or cand > nxt:
            nxt = cand
    return nxt.isoformat()


def load_series(conn, symbol: str, upto: str | None = None) -> Series:
    upto = upto or as_of(conn)
    rows = conn.execute("SELECT * FROM bars WHERE symbol=? AND date<=? ORDER BY date", (symbol, upto)).fetchall()
    if not rows:
        raise TradeError(f"No data for {symbol}", 404)
    return Series(symbol, [r["date"] for r in rows], [r["open"] for r in rows], [r["high"] for r in rows],
                  [r["low"] for r in rows], [r["close"] for r in rows], [r["volume"] for r in rows], rows[-1]["source"])  # label = source of the latest bar


def earnings_for(conn, symbol: str, on: str) -> tuple[str | None, list[str]]:
    rows = conn.execute("SELECT date, kind FROM earnings WHERE symbol=?", (symbol,)).fetchall()
    dates = sorted(r["date"] for r in rows if r["date"])
    past = [d for d in dates if d <= on]
    future = [d for d in dates if d > on]
    return (future[0] if future else None), past


def bars_on(conn, session: str) -> dict[str, dict]:
    return {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM bars WHERE date=?", (session,))}


# ---------- rulesets ----------

def active_ruleset(conn) -> dict | None:
    r = conn.execute("SELECT * FROM rulesets ORDER BY version DESC LIMIT 1").fetchone()
    return _rs(r) if r else None


def _rs(r) -> dict:
    return {"version": r["version"], "name": r["name"], "source_text": r["source_text"],
            "params": json.loads(r["params_json"]), "interpretations": json.loads(r["interpretations_json"]),
            "confirmed_at": r["confirmed_at"]}


def list_rulesets(conn) -> list[dict]:
    return [_rs(r) for r in conn.execute("SELECT * FROM rulesets ORDER BY version DESC")]


def draft_ruleset(text: str | None = None) -> dict:
    parsed = parse_strategy(text or DEFAULT_TEXT)
    return {"version": None, "name": "Draft (not confirmed)", "source_text": text or DEFAULT_TEXT, **parsed, "confirmed_at": None}


def confirm_ruleset(conn, text: str, params: dict, interpretations: list, name: str) -> dict:
    p = RuleParams.from_dict(params)
    nxt = (conn.execute("SELECT MAX(version) FROM rulesets").fetchone()[0] or 0) + 1
    conn.execute("INSERT INTO rulesets(version,name,source_text,params_json,interpretations_json,confirmed_at) VALUES(?,?,?,?,?,?)",
                 (nxt, name or f"{STRATEGIES[p.strategy]['name']} v{nxt}", text, json.dumps(asdict(p)), json.dumps(interpretations),
                  utcnow().isoformat(timespec="seconds")))
    return active_ruleset(conn)


def current_params(conn) -> tuple[RuleParams, dict]:
    rs = active_ruleset(conn) or draft_ruleset()
    return RuleParams.from_dict(rs["params"]), rs


# ---------- screening ----------

def data_timestamp(ev: dict, source: str) -> str:
    return f"{ev['date']} 16:00 ET close (completed daily bar) | {source}"


def evaluate_symbol(conn, symbol: str, params: RuleParams | None = None) -> dict:
    p = params or current_params(conn)[0]
    if as_of(conn) is None:
        raise TradeError("No market data yet. Refresh live data first.", 409)
    s = load_series(conn, symbol)
    nxt, past = earnings_for(conn, symbol, s.dates[-1])
    dq = (db.kv_get(conn, "data_quality") or {}).get("symbols", {}).get(symbol) or {}
    issue = dq.get("latest_issue")
    ev = evaluate_at(s, len(s.dates) - 1, p, nxt, past, expected_date=as_of(conn),
                     data_issue=f"data quality: {issue['kind'].replace('_', ' ')} ({issue['detail']})"
                     if issue and issue.get("date") == s.dates[-1] else None)
    ev["data_timestamp"] = data_timestamp(ev, s.source)
    return ev


def screen(conn) -> dict:
    p, rs = current_params(conn)
    rows, errors = [], {}
    if as_of(conn) is not None:
        for sym in db.kv_get(conn, "watchlist", []):
            try:
                rows.append(evaluate_symbol(conn, sym, p))
            except TradeError as exc:
                errors[sym] = str(exc)
        track_forward(conn, rows, rs)
    dq = db.kv_get(conn, "data_quality") or {}
    quality = {sym: {"ok": q["ok"], "issues": q["issues"][-3:]} for sym, q in (dq.get("symbols") or {}).items()}
    return {"as_of": as_of(conn), "ruleset": rs, "results": rows, "errors": errors,
            "data_mode": db.data_mode(), "needs_refresh": as_of(conn) is None, "quality": quality,
            "data_provider": dq.get("provider")}


def all_series(conn) -> dict:
    out = {}
    for sym in db.kv_get(conn, "watchlist", []):
        try:
            out[sym] = load_series(conn, sym)
        except TradeError:
            pass
    return out


def track_forward(conn, rows: list[dict], rs: dict) -> None:
    """Record today's qualifying setups (idempotent) and follow recorded ones on new bars."""
    src = rows[0]["data_timestamp"].split(" | ")[-1] if rows else ""
    for ev in forward.record(conn, rows, rs, src, eligible_from=first_fillable_session(conn) if rows else None):
        alerts.emit(conn, "signal", f"New setup: {ev['symbol']} {ev['status'].replace('_', ' ').lower()}",
                    f"{ev['symbol']} met rules v{rs['version']} on the {ev['date']} close. Stop {ev['suggested_stop']}, "
                    f"planned exit {ev['plan']['exit_date']}.", "info", ev["symbol"], f"signal:{ev['symbol']}:{ev['date']}:{rs['version']}")
    forward.update(conn, all_series(conn))


def forward_summary(conn) -> dict:
    _, rs = current_params(conn)
    if as_of(conn) is not None:
        forward.update(conn, all_series(conn))
    return forward.summary(conn, rs["version"])


# ---------- live data ----------

_refresh_lock = threading.Lock()


def run_live_refresh(trigger: str = "manual") -> dict:
    """Fetch outside any transaction, then apply in one short transaction.
    Serialised so the scheduler and a button click can't interleave."""
    if db.data_mode() != "live":
        raise TradeError("Live refresh is only available in Live data mode.", 409)
    with _refresh_lock:
        c = db.connect()
        try:
            symbols = db.kv_get(c, "watchlist", [])
            have = {r["symbol"] for r in c.execute("SELECT symbol FROM sectors WHERE sector IS NOT NULL")}
            held = [r["symbol"] for r in c.execute("""SELECT DISTINCT ord.symbol FROM orders ord JOIN journal j ON j.order_id=ord.id
                                                      JOIN journal_outcomes o ON o.journal_id=j.id WHERE o.status='open'""")]
        finally:
            c.close()
        fetched = live_data.fetch(symbols, need_sector=set(symbols) - have, split_symbols=sorted(set(symbols) | set(held)))
        with db.tx() as conn:
            res = live_refresh(conn, fetched)
            mode = broker_mode(conn)
            failed = [k for k, v in res["report"].items() if not v["ok"]]
            db.kv_set(conn, "last_refresh", {"at": res["fetched_at"], "trigger": trigger, "as_of": res["as_of"],
                                             "events": res["events"], "failed": failed})
            if failed:
                alerts.emit(conn, "data", f"Data fetch failed: {', '.join(failed)}",
                            "; ".join(f"{k}: {res['report'][k]['error']}" for k in failed) + ". Cached prices kept; affected tickers show as stale.",
                            "warn", None, f"fetch:{res['fetched_at'][:10]}:{','.join(failed)}")
        res["broker"] = after_refresh(mode)
        return res


def after_refresh(mode: str) -> dict:
    """Sync broker orders and reconcile, outside any transaction. Failures are reported, never fatal."""
    from . import broker_sync
    out = {}
    if factory.is_external(mode) and factory.connected(mode):
        try:
            out["sync"] = broker_sync.sync()
        except Exception as exc:
            out["sync_error"] = f"{type(exc).__name__}: {exc}"[:200]
    try:
        out["reconciliation"] = broker_sync.reconcile(mode)
    except Exception as exc:
        out["reconciliation_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


def live_refresh(conn, fetched: dict | None = None) -> dict:
    if db.data_mode() != "live":
        raise TradeError("Live refresh is only available in Live data mode.", 409)
    before = as_of(conn)
    if fetched is None:
        fetched = live_data.fetch(db.kv_get(conn, "watchlist", []))
    res = live_data.store(conn, fetched)
    latest = res["latest"]
    events = []
    if latest and before and latest > before:
        # Run the paper simulator over each new real session, in order.
        for (d,) in conn.execute("SELECT DISTINCT date FROM bars WHERE date>? AND date<=? ORDER BY date", (before, latest)).fetchall():
            events += sim.process_session(conn, d, bars_on(conn, d), plan_exits(conn))
            db.kv_set(conn, "as_of", d)
    if latest and (before is None or latest > before):
        db.kv_set(conn, "as_of", latest)
    refresh_outcomes(conn)
    if active_ruleset(conn):
        screen(conn)  # records any newly qualifying setups for the forward test
    return {**res, "as_of": as_of(conn), "events": events}


def set_watchlist(conn, symbols: list[str]) -> list[str]:
    if db.data_mode() != "live":
        raise TradeError("The demo watchlist is fixed to its four simulated tickers. Switch to Live data to edit it.", 409)
    clean = []
    for s in symbols:
        v = live_data.validate_symbol(s)
        if v not in clean:
            clean.append(v)
    if not 1 <= len(clean) <= 12:
        raise TradeError("Keep between 1 and 12 tickers on the watchlist.")
    exposed = {r["symbol"] for r in conn.execute(
        "SELECT symbol, id FROM orders WHERE purpose='entry' AND status NOT IN ('rejected')").fetchall()
        if conn.execute("SELECT status FROM orders WHERE id=?", (r["id"],)).fetchone()["status"] in ("pending", "partially_filled")
        or sim._open_qty(conn, r["id"]) > 0}
    kept_out = sorted(exposed - set(clean))
    if kept_out:
        raise TradeError(f"Close or cancel your {', '.join(kept_out)} position/order before removing it: "
                         "its prices are needed to run the stop and planned exit.", 409)
    db.kv_set(conn, "watchlist", clean)
    return clean


def chart(conn, symbol: str, sessions: int = 90) -> dict:
    p, _ = current_params(conn)
    s = load_series(conn, symbol)
    start = max(0, len(s.dates) - sessions)
    lines = [{"name": o["name"], "values": [{"time": s.dates[i], "value": round(o["values"][i], 4)}
                                            for i in range(start, len(s.dates)) if o["values"][i] is not None]}
             for o in overlays(s, p)]
    return {"symbol": symbol, "source": s.source, "strategy": p.strategy, "strategy_name": STRATEGIES[p.strategy]["name"],
            "overlays": lines,
            "bars": [{"time": s.dates[i], "open": s.open[i], "high": s.high[i], "low": s.low[i], "close": s.close[i],
                      "volume": s.volume[i]} for i in range(start, len(s.dates))]}


# ---------- account ----------

def broker_mode(conn) -> str:
    return db.kv_get(conn, "broker_mode", "simulator")


def account(conn) -> dict:
    start = db.kv_get(conn, "account")["starting_equity"]
    rows = conn.execute("""SELECT o.symbol, o.side, f.qty, f.price FROM fills f JOIN orders o ON o.id=f.order_id
                           WHERE o.broker='simulator'""").fetchall()
    cash, pos = start, {}
    for r in rows:
        sign = 1 if r["side"] == "buy" else -1
        cash -= sign * r["qty"] * r["price"]
        pos[r["symbol"]] = pos.get(r["symbol"], 0) + sign * r["qty"]
    mv = 0.0
    positions = []
    for sym, q in pos.items():
        if q:
            last = _last_close(conn, sym)
            mv += q * last
            positions.append({"symbol": sym, "qty": q, "last": last, "market_value": round(q * last, 2)})
    committed = sum((r["qty"] - r["filled_qty"]) * (r["limit_price"] or _last_close(conn, r["symbol"]))
                    for r in conn.execute("SELECT * FROM orders WHERE broker='simulator' AND purpose='entry' AND status IN ('pending','partially_filled')"))
    return {"mode": broker_mode(conn), "starting_equity": start, "cash": round(cash, 2), "equity": round(cash + mv, 2),
            "buying_power": round(cash - committed, 2), "positions": positions}


def _last_close(conn, sym):
    """Latest known close (skips provider gaps where close is missing)."""
    r = conn.execute("SELECT close FROM bars WHERE symbol=? AND date<=? AND close IS NOT NULL ORDER BY date DESC LIMIT 1",
                     (sym, as_of(conn))).fetchone()
    return r["close"] if r else 0.0


def broker_account() -> dict | None:
    """Live Alpaca account (equity, buying power, flags), or None in simulator mode."""
    c = db.connect()
    try:
        mode = broker_mode(c)
    finally:
        c.close()
    if not factory.is_external(mode):
        return None
    try:
        return factory.adapter(mode).account()
    except BrokerError as exc:
        raise TradeError(f"Couldn't read the {factory.LABELS[mode]} account: {exc}", 502)


def sizing_account(conn, broker_acct: dict | None) -> tuple[float, float]:
    """(equity, buying power) used for sizing and risk: the broker's numbers when a broker is connected."""
    if broker_acct:
        return broker_acct["equity"], broker_acct["buying_power"]
    a = account(conn)
    return a["equity"], a["buying_power"]


def run_risk(conn, *, symbol, qty, entry, stop, broker_acct) -> dict:
    equity, bp = sizing_account(conn, broker_acct)
    return risk.evaluate(conn, symbol=symbol, qty=qty, entry=entry, stop=stop, equity=equity, buying_power=bp,
                         broker=broker_mode(conn), broker_account=broker_acct, session=as_of(conn))


# ---------- previews and orders ----------

def make_preview(conn, symbol: str, overrides: dict | None = None, broker_acct: dict | None = None) -> dict:
    overrides = overrides or {}
    p, rs = current_params(conn)
    if rs["version"] is None:
        raise TradeError("Confirm and save your rule set first; previews record the rule version.", 409)
    ev = evaluate_symbol(conn, symbol, p)
    if ev["status"] not in ("QUALIFIES", "QUALIFIES_WITH_WARNING"):
        raise TradeError(f"{symbol} does not qualify under rules v{rs['version']} ({ev['status']}); no order preview.", 409)
    equity, _bp = sizing_account(conn, broker_acct)
    entry = float(overrides.get("limit_price") or ev["entry_assumption"])
    order_type = overrides.get("order_type", "market")
    if order_type not in ("market", "limit"):
        raise TradeError("order_type must be market or limit")
    stop = float(overrides.get("stop_price") or ev["suggested_stop"])
    fill_session = first_fillable_session(conn)
    plan = ev["plan"]
    if fill_session != plan["entry_date"]:
        # The signal's planned next-open entry has already printed: shift the whole holding window to the first fair open.
        sess = [fill_session] + [d.isoformat() for d in next_sessions(date.fromisoformat(fill_session), p.hold_sessions - 1)]
        plan = {"entry_date": sess[0], "exit_date": sess[-1], "sessions": sess,
                "shifted_from": ev["plan"]["entry_date"], "shift_reason": "the signal's next open had already traded when this preview was made"}
    risk_budget = equity * p.risk_per_trade_pct / 100
    per_share = entry - stop
    if per_share <= 0:
        raise TradeError("Stop must be below the entry assumption for a long position.")
    sized = min(math.floor(risk_budget / per_share), math.floor(equity * p.max_position_pct / 100 / entry))
    qty = int(overrides.get("qty") or sized)
    value = qty * entry
    slip_cost = value * 2 * sim.SLIPPAGE_BPS / 10000
    est_loss = qty * per_share + slip_cost
    pid = "pv_" + uuid.uuid4().hex[:16]
    preview = {
        "id": pid, "symbol": symbol, "side": "buy", "qty": qty, "order_type": order_type,
        "limit_price": entry if order_type == "limit" else None,
        "entry_assumption": round(entry, 2),
        "entry_assumption_basis": "limit price" if order_type == "limit" else f"last completed close ({ev['date']}); a market order fills at the {first_fillable_session(conn)} open",
        "stop_price": round(stop, 2), "suggested_stop": ev["suggested_stop"], "suggested_qty": sized,
        "est_position_value": round(value, 2), "pct_of_equity": round(value / equity * 100, 2),
        "est_loss_if_stopped": round(est_loss, 2), "est_loss_pct_equity": round(est_loss / equity * 100, 3),
        "risk_budget": round(risk_budget, 2), "equity": equity,
        "sizing": {"formula": "qty = min(floor(risk budget / (entry - stop)), floor(equity x max position % / entry))",
                   "risk_per_trade_pct": p.risk_per_trade_pct, "max_position_pct": p.max_position_pct,
                   "per_share_risk": round(per_share, 4), "slippage_assumption_bps_each_side": sim.SLIPPAGE_BPS},
        "plan": plan, "status_at_preview": ev["status"], "fills_at_open_of": fill_session,
        "earnings_warning": next((r for r in ev["rules"] if r["id"] == "earnings" and r["result"] == "warn"), None),
        "ruleset_version": rs["version"], "data_timestamp": ev["data_timestamp"], "mode": broker_mode(conn),
        "fill_disclaimer": ("Estimates assume a fill at the entry assumption and an exit exactly at the stop. Actual fills can differ: "
                            "the next open can gap, slippage varies, orders can fill partially, and a stop becomes a market order "
                            "that can fill below the stop price."),
        "expires_at": (utcnow() + timedelta(minutes=PREVIEW_TTL_MIN)).isoformat(timespec="seconds"),
        "evaluation": ev,
        "equity_source": f"{factory.LABELS.get(broker_mode(conn), 'broker')} account" if broker_acct else "local paper account",
        "risk": run_risk(conn, symbol=symbol, qty=qty, entry=entry, stop=stop, broker_acct=broker_acct),
    }
    conn.execute("INSERT INTO previews(id,payload_json,created_at) VALUES(?,?,?)",
                 (pid, json.dumps(preview), utcnow().isoformat(timespec="seconds")))
    return preview


def order_view(conn, oid: str) -> dict:
    o = dict(conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone())
    o["fills"] = [dict(f) for f in conn.execute("SELECT session_date,qty,price,note FROM fills WHERE order_id=? ORDER BY id", (oid,))]
    o["children"] = [dict(c) for c in conn.execute("SELECT id,purpose,status,status_reason,qty,filled_qty,avg_fill_price,stop_price FROM orders WHERE parent_id=? ORDER BY created_at", (oid,))]
    return o


def _validate_submission(conn, preview_id: str, ack_earnings: bool, risk_overrides: dict | None = None,
                         broker_acct: dict | None = None, live_confirm: str | None = None) -> dict:
    dup = conn.execute("SELECT id FROM orders WHERE client_order_id=?", (preview_id,)).fetchone()
    if dup:
        raise TradeError("Duplicate submission blocked: this preview was already submitted.", 409,
                         {"order": order_view(conn, dup["id"]), "duplicate": True})
    row = conn.execute("SELECT payload_json FROM previews WHERE id=?", (preview_id,)).fetchone()
    if not row:
        raise TradeError("Unknown preview. Open a new order preview.", 404)
    pv = json.loads(row["payload_json"])
    if datetime.fromisoformat(pv["expires_at"]) < utcnow():
        raise TradeError("Preview expired; open a fresh one so prices and rules are current.", 410)
    rs = active_ruleset(conn)
    if not rs or rs["version"] != pv["ruleset_version"]:
        raise TradeError(f"Rules changed since this preview (preview v{pv['ruleset_version']}, active "
                         f"v{rs['version'] if rs else '-'}). Open a fresh preview.", 409)
    if pv["data_timestamp"] != evaluate_symbol(conn, pv["symbol"])["data_timestamp"]:
        raise TradeError("Market data advanced since this preview; open a fresh one.", 409)
    if pv["mode"] != broker_mode(conn):
        raise TradeError("Broker mode changed since this preview; open a fresh one.", 409)
    if db.environment() == "real":
        if not factory.is_external(pv["mode"]) or not factory.capabilities(pv["mode"])["real_money"]:
            raise TradeError("Real-money mode only sends orders through a broker approved for real money (Alpaca).", 409)
        if (live_confirm or "").strip().upper() != pv["symbol"]:
            raise TradeError(f"REAL MONEY: type {pv['symbol']} to confirm this order.", 422)
    if pv["earnings_warning"] and not ack_earnings:
        raise TradeError("This setup has earnings inside the holding window. Acknowledge the warning to submit.", 422)
    for r in conn.execute("SELECT * FROM orders WHERE symbol=? AND purpose='entry' AND status!='rejected'", (pv["symbol"],)).fetchall():
        busy = r["status"] in OPEN_STATUSES
        if r["broker"] == "simulator":
            busy = busy or sim._open_qty(conn, r["id"]) > 0
        elif r["status"] == "filled":
            # Broker-held positions: treat as open until their planned exit date has passed.
            j = conn.execute("SELECT plan_json FROM journal WHERE order_id=?", (r["id"],)).fetchone()
            busy = bool(j) and json.loads(j["plan_json"])["exit_date"] >= as_of(conn)
        if busy:
            raise TradeError(f"You already have a working order or open position in {pv['symbol']}.", 409,
                             {"order": order_view(conn, r["id"]), "duplicate": True})
    # Risk is re-evaluated now (positions, P&L and the kill switch may have changed since the preview).
    rk = run_risk(conn, symbol=pv["symbol"], qty=pv["qty"], entry=pv["entry_assumption"], stop=pv["stop_price"],
                  broker_acct=broker_acct)
    used = risk.enforce(rk, risk_overrides)
    return {**pv, "_ruleset": rs, "_risk": rk, "_overrides": used}


def _write_journal(conn, oid: str, pv: dict, thesis: str, ack_earnings: bool) -> None:
    ev, rs = pv["evaluation"], pv["_ruleset"]
    plan = {**pv["plan"], "stop": pv["stop_price"], "suggested_stop": pv["suggested_stop"], "qty": pv["qty"],
            "suggested_qty": pv["suggested_qty"], "risk_budget": pv["risk_budget"],
            "est_loss_if_stopped": pv["est_loss_if_stopped"], "entry_assumption": pv["entry_assumption"],
            "earnings_warning_acknowledged": bool(pv["earnings_warning"] and ack_earnings),
            "status_at_entry": ev["status"], "fills_at_open_of": pv.get("fills_at_open_of"),
            "risk_checks": pv["_risk"]["checks"], "risk_limits_version": pv["_risk"]["limits_version"],
            "risk_overrides": [{"check": c["id"], "observed": c["observed"], "threshold": c["threshold"], "reason": c["reason"]}
                               for c in pv["_overrides"]]}
    cur = conn.execute("""INSERT INTO journal(order_id,symbol,created_at,thesis,ruleset_version,ruleset_json,evidence_json,
                          data_timestamp,order_json,plan_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (oid, pv["symbol"], utcnow().isoformat(timespec="seconds"), thesis.strip() or auto_thesis(ev),
                        rs["version"], json.dumps(rs), json.dumps(ev), pv["data_timestamp"],
                        json.dumps({k: pv[k] for k in ("symbol", "side", "qty", "order_type", "limit_price", "stop_price", "mode")} | {"order_id": oid}),
                        json.dumps(plan)))
    conn.execute("INSERT INTO journal_outcomes(journal_id,status,updated_at) VALUES(?, 'open', ?)",
                 (cur.lastrowid, utcnow().isoformat(timespec="seconds")))


def submit(preview_id: str, confirm: bool, thesis: str, ack_earnings: bool = False, risk_overrides: dict | None = None,
           live_confirm: str | None = None) -> dict:
    if not confirm:
        raise TradeError("Submission requires explicit confirmation.")
    acct = broker_account()          # network read before taking the write lock
    with db.tx() as conn:
        pv = _validate_submission(conn, preview_id, ack_earnings, risk_overrides, acct, live_confirm)
        risk.log_overrides(conn, preview_id, pv["symbol"], pv["_overrides"])
        req = OrderRequest(preview_id, pv["symbol"], "buy", pv["qty"], pv["order_type"], pv["limit_price"], pv["stop_price"])
        if pv["mode"] == "simulator":
            res = sim.submit(conn, req, account(conn)["buying_power"], pv["entry_assumption"], eligible_from=first_fillable_session(conn))
            if res.status != "rejected":
                _write_journal(conn, res.broker_order_id, pv, thesis, ack_earnings)
            return {"order": order_view(conn, res.broker_order_id), "duplicate": False}
        # External broker: reserve the order row (the UNIQUE client_order_id blocks duplicates) and commit before the network call,
        # so a slow broker never holds the database write lock.
        oid = sim.insert_order(conn, client_order_id=preview_id, symbol=req.symbol, side="buy", qty=req.qty,
                               order_type=req.order_type, limit_price=req.limit_price, stop_price=req.stop_price,
                               purpose="entry", status="pending", reason=f"sending to {factory.LABELS[pv['mode']]}")
        conn.execute("UPDATE orders SET broker=?, broker_order_id=NULL WHERE id=?", (pv["mode"], oid))
        _write_journal(conn, oid, pv, thesis, ack_earnings)
    try:
        res = factory.adapter(pv["mode"]).submit(req)
        status, reason, boid = res.status, res.status_reason, res.broker_order_id
    except BrokerError as exc:
        status, reason, boid = "rejected", f"broker error: {exc}", None
    with db.tx() as conn:
        conn.execute("UPDATE orders SET status=?, status_reason=?, broker_order_id=?, updated_at=? WHERE id=?",
                     (status, reason, boid, sim.now(), oid))
        refresh_outcomes(conn)
        return {"order": order_view(conn, oid), "duplicate": False}


def auto_thesis(ev: dict) -> str:
    core = STRATEGIES[ev.get("strategy", "ema_pullback")]["core"]
    why = "; ".join(f"{r['name'].lower()} ({r['observed']})" for r in ev["rules"] if r["id"] in core)
    return (f"{ev['symbol']} {ev['date']}, {ev.get('strategy_name', 'EMA pullback')}: {why}. "
            f"Plan: enter next open, exit {ev['plan']['exit_date']} close or at the stop {ev['suggested_stop']}.")


def cancel_order(oid: str) -> dict:
    c = db.connect()
    try:
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    finally:
        c.close()
    if not o:
        raise TradeError("Unknown order", 404)
    if o["status"] not in OPEN_STATUSES:
        raise TradeError(f"Order is {o['status']}; only working orders can be canceled.", 409)
    if factory.is_external(o["broker"]) and o["broker_order_id"]:
        try:
            factory.adapter(o["broker"]).cancel(o["broker_order_id"])  # outside any transaction
        except BrokerError as exc:
            raise TradeError(f"{factory.LABELS[o['broker']]} refused the cancel: {exc}", 502)
    with db.tx() as conn:
        return cancel(conn, oid)


def cancel(conn, oid: str) -> dict:
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise TradeError("Unknown order", 404)
    if o["status"] not in OPEN_STATUSES:
        raise TradeError(f"Order is {o['status']}; only working orders can be canceled.", 409)
    if o["purpose"] == "entry" and o["filled_qty"] > 0:
        sim._set_status(conn, oid, "canceled", f"remainder canceled by user; {o['filled_qty']} shares already filled")
    elif o["purpose"] == "entry":
        sim._close_children(conn, oid, "canceled by user")
        sim._set_status(conn, oid, "canceled", "canceled by user")
    else:
        sim._set_status(conn, oid, "canceled", "canceled by user")
    refresh_outcomes(conn)
    return order_view(conn, oid)


def close_position(conn, entry_id: str) -> dict:
    if sim._open_qty(conn, entry_id) <= 0:
        raise TradeError("No open position for this order.", 409)
    if conn.execute("SELECT 1 FROM orders WHERE parent_id=? AND purpose='manual_exit' AND status='pending'", (entry_id,)).fetchone():
        raise TradeError("A manual exit is already working.", 409)
    e = conn.execute("SELECT * FROM orders WHERE id=?", (entry_id,)).fetchone()
    if e["broker"] != "simulator":
        raise TradeError("Close broker-held positions in your Alpaca account; broker-managed exits are on the roadmap.", 409)
    n = conn.execute("SELECT COUNT(*) FROM orders WHERE parent_id=? AND purpose='manual_exit'", (entry_id,)).fetchone()[0]
    when = first_fillable_session(conn)
    sim.insert_order(conn, client_order_id=f"{e['client_order_id']}:manual:{n + 1}", symbol=e["symbol"], side="sell",
                     qty=sim._open_qty(conn, entry_id), order_type="market", limit_price=None, stop_price=None,
                     purpose="manual_exit", parent_id=entry_id, reason=f"manual exit; fills at the {when} open", eligible_from=when)
    return order_view(conn, entry_id)


# ---------- simulator clock ----------

def plan_exits(conn) -> dict[str, str]:
    """Planned exits the local simulator is responsible for (never broker-held orders)."""
    out = {}
    for j in conn.execute("""SELECT j.order_id, j.plan_json FROM journal j JOIN journal_outcomes o ON o.journal_id=j.id
                             JOIN orders ord ON ord.id=j.order_id WHERE o.status='open' AND ord.broker='simulator'
                             AND COALESCE(ord.style,'swing')='swing'"""):
        out[j["order_id"]] = json.loads(j["plan_json"])["exit_date"]
    return out


def advance(conn) -> dict:
    if db.data_mode() != "demo":
        raise TradeError("In Live mode, sessions arrive from real market data. Use Refresh live data.", 409)
    cur = as_of(conn)
    nxt = conn.execute("SELECT MIN(date) FROM bars WHERE date>?", (cur,)).fetchone()[0]
    if not nxt:
        raise TradeError("End of simulated data reached. Use Reset demo to start over.", 409)
    events = sim.process_session(conn, nxt, bars_on(conn, nxt), plan_exits(conn))
    db.kv_set(conn, "as_of", nxt)
    refresh_outcomes(conn)
    if active_ruleset(conn):
        screen(conn)
    return {"as_of": nxt, "events": events or [f"{nxt}: session simulated, no order activity"]}


# ---------- journal ----------

def refresh_outcomes(conn) -> None:
    for j in conn.execute("SELECT j.*, o.status AS ostatus FROM journal j JOIN journal_outcomes o ON o.journal_id=j.id WHERE o.status='open'").fetchall():
        entry = conn.execute("SELECT * FROM orders WHERE id=?", (j["order_id"],)).fetchone()
        # Simulator and broker orders are handled the same way: both record fills in the fills table
        # (broker fills arrive through broker_sync), so P&L and adherence come from actual fills.
        buys = conn.execute("SELECT * FROM fills WHERE order_id=? ORDER BY id", (entry["id"],)).fetchall()
        sells = conn.execute("""SELECT f.*, o.purpose FROM fills f JOIN orders o ON o.id=f.order_id
                                WHERE o.parent_id=? AND o.side='sell' ORDER BY f.id""", (entry["id"],)).fetchall()
        bq = sum(b["qty"] for b in buys)
        sq = sum(s["qty"] for s in sells)
        ts = utcnow().isoformat(timespec="seconds")
        if bq == 0:
            if entry["status"] in ("canceled", "rejected"):
                conn.execute("UPDATE journal_outcomes SET status='not_filled', updated_at=? WHERE journal_id=?", (ts, j["id"]))
            continue
        cost = sum(b["qty"] * b["price"] for b in buys)
        entry_px = cost / bq
        prev_entry = conn.execute("SELECT entry_date FROM journal_outcomes WHERE journal_id=?", (j["id"],)).fetchone()["entry_date"]
        if prev_entry is None:
            alerts.emit(conn, "fill", f"Filled: {entry['symbol']} {bq} @ {entry_px:.2f}",
                        f"Entry filled on {buys[0]['session_date']} ({entry['broker']}). Stop and planned exit are active.",
                        "info", entry["symbol"], f"fill:{entry['id']}")
        conn.execute("UPDATE journal_outcomes SET entry_date=?, entry_price=?, qty=?, updated_at=? WHERE journal_id=?",
                     (buys[0]["session_date"], round(entry_px, 4), bq, ts, j["id"]))
        if sq < bq or entry["status"] in OPEN_STATUSES:
            continue
        proceeds = sum(s["qty"] * s["price"] for s in sells)
        pnl = proceeds - cost
        reason = sells[-1]["purpose"]
        plan = json.loads(j["plan_json"])
        adherence = adherence_checks(json.loads(j["evidence_json"]), plan, reason, sells[-1]["session_date"])
        alerts.emit(conn, "close", f"Closed: {entry['symbol']} {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f} ({reason.replace('_', ' ')})",
                    f"Exited {sq} @ {proceeds / sq:.2f} on {sells[-1]['session_date']}. Followed plan: {adherence['score']}.",
                    "warn" if reason == "stop" else "info", entry["symbol"], f"close:{entry['id']}")
        conn.execute("""UPDATE journal_outcomes SET status='closed', exit_date=?, exit_price=?, exit_reason=?, realized_pnl=?,
                        return_pct=?, adherence_json=?, updated_at=? WHERE journal_id=?""",
                     (sells[-1]["session_date"], round(proceeds / sq, 4), reason, round(pnl, 2), round(pnl / cost * 100, 3),
                      json.dumps(adherence), ts, j["id"]))


def adherence_checks(ev: dict, plan: dict, exit_reason: str, exit_date: str) -> dict:
    checks = [
        {"check": "Entered only on a qualifying signal", "ok": plan["status_at_entry"] in ("QUALIFIES", "QUALIFIES_WITH_WARNING"),
         "detail": f"status at entry: {plan['status_at_entry']}"},
        {"check": "Stop placed at the planned level", "ok": abs(plan["stop"] - plan["suggested_stop"]) < 0.005,
         "detail": f"stop {plan['stop']} vs planned {plan['suggested_stop']}"},
        {"check": "Position size within risk budget", "ok": plan["est_loss_if_stopped"] <= plan["risk_budget"] * 1.05,
         "detail": f"est. loss ${plan['est_loss_if_stopped']:,.2f} vs budget ${plan['risk_budget']:,.2f}"},
        {"check": "Exited by the plan (stop or planned date)", "ok": exit_reason in ("stop", "plan_exit", "target", "flat"),
         "detail": {"stop": "stop filled", "plan_exit": f"planned exit on {exit_date}", "target": "profit target filled",
                    "flat": f"flat by {plan.get('flat_by', 'the close')} on {exit_date}",
                    "manual_exit": f"manual exit on {exit_date}, planned {plan['exit_date']}"}.get(exit_reason, exit_reason)},
    ]
    if "risk_overrides" in plan:
        ov = plan["risk_overrides"]
        checks.append({"check": "Passed pre-trade risk checks without overrides", "ok": not ov,
                       "detail": "no overrides" if not ov else "overrode " + "; ".join(f"{o['check']} ({o['observed']}): {o['reason']}" for o in ov)})
    if any(r["id"] == "earnings" and r["result"] == "warn" for r in ev["rules"]):
        checks.append({"check": "Earnings warning acknowledged before entry", "ok": plan.get("earnings_warning_acknowledged", False),
                       "detail": "earnings inside holding window"})
    ok = sum(c["ok"] for c in checks)
    return {"followed": ok == len(checks), "score": f"{ok}/{len(checks)}", "checks": checks}


def journal(conn) -> list[dict]:
    out = []
    for j in conn.execute("SELECT j.*, o.* FROM journal j JOIN journal_outcomes o ON o.journal_id=j.id ORDER BY j.id DESC"):
        d = dict(j)
        for k in ("ruleset_json", "evidence_json", "order_json", "plan_json", "adherence_json"):
            d[k.removesuffix("_json")] = json.loads(d.pop(k)) if d.get(k) else None
        has_order = conn.execute("SELECT 1 FROM orders WHERE id=?", (d["order_id"],)).fetchone()
        d["order_detail"] = order_view(conn, d["order_id"]) if has_order else {"status": "unknown", "children": [], "fills": []}
        out.append(d)
    return out


def add_review_note(conn, journal_id: int, note: str) -> None:
    conn.execute("UPDATE journal_outcomes SET review_note=?, updated_at=? WHERE journal_id=?",
                 (note, utcnow().isoformat(timespec="seconds"), journal_id))
