"""Pre-trade risk checks, evaluated on every preview and again, authoritatively, at submit.

Hard blocks stop the order outright: kill switch on, broker account blocked,
restricted symbol, not enough buying power, daily loss limit hit.
Soft blocks need a written reason to override: too many open positions, too
much total open risk ("portfolio heat"), sector concentration, and the
pattern-day-trader count for small accounts. Every override is stored write-once
with the observed value, the threshold and the reason, and copied into the
journal entry. Limits are versioned like strategy rules, so you can always
reconstruct which limits applied to a past order.

Model follows the hard/soft block design in the trading-operations plugin's
pre-trade compliance guidance (hard blocks for non-negotiable conditions,
documented soft-block overrides, rule version control).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import db

DEFAULTS = {
    "max_open_positions": 5,
    "max_portfolio_heat_pct": 2.5,     # sum of (entry - stop) x qty over equity, including the new order
    "max_sector_pct": 40.0,            # market value in one sector over equity, including the new order
    "daily_loss_limit_pct": 2.0,       # realized today + open P&L; at or beyond -X% of equity, new entries stop
    "pdt_equity_min": 25000.0,         # FINRA pattern-day-trader minimum equity
    "restricted": [],                  # symbols that may not be bought
}
MIN_REASON = 10

DEMO_SECTORS = {"NVDA": "Technology", "SNDK": "Technology", "COIN": "Financial Services", "IREN": "Financial Services"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- limits (versioned) ----------

def limits(conn) -> dict:
    r = conn.execute("SELECT * FROM risk_limits ORDER BY version DESC LIMIT 1").fetchone()
    if not r:
        return {"version": 0, "params": dict(DEFAULTS), "note": "defaults", "created_at": None}
    return {"version": r["version"], "params": {**DEFAULTS, **json.loads(r["params_json"])}, "note": r["note"], "created_at": r["created_at"]}


def validate(params: dict) -> dict:
    p = {**DEFAULTS, **{k: v for k, v in params.items() if k in DEFAULTS}}
    rules = [("max_open_positions", 1, 100), ("max_portfolio_heat_pct", 0.1, 50), ("max_sector_pct", 1, 100),
             ("daily_loss_limit_pct", 0.1, 50), ("pdt_equity_min", 0, 1e9)]
    for k, lo, hi in rules:
        p[k] = float(p[k]) if k != "max_open_positions" else int(p[k])
        if not lo <= p[k] <= hi:
            raise ValueError(f"{k} must be between {lo} and {hi}")
    p["restricted"] = sorted({s.strip().upper() for s in p["restricted"] if s and s.strip()})
    return p


def save_limits(conn, params: dict, note: str) -> dict:
    p = validate(params)
    nxt = (conn.execute("SELECT MAX(version) FROM risk_limits").fetchone()[0] or 0) + 1
    conn.execute("INSERT INTO risk_limits(version,params_json,note,created_at) VALUES(?,?,?,?)",
                 (nxt, json.dumps(p), note or f"risk limits v{nxt}", _now()))
    return limits(conn)


def history(conn) -> list[dict]:
    return [{"version": r["version"], "params": json.loads(r["params_json"]), "note": r["note"], "created_at": r["created_at"]}
            for r in conn.execute("SELECT * FROM risk_limits ORDER BY version DESC")]


def kill_switch(conn) -> dict:
    return db.kv_get(conn, "kill_switch", {"on": False})


def set_kill_switch(conn, on: bool, reason: str) -> dict:
    if on and len((reason or "").strip()) < 3:
        raise ValueError("Say why you are stopping trading (for the record).")
    ks = {"on": on, "reason": reason.strip() if on else None, "at": _now()}
    db.kv_set(conn, "kill_switch", ks)
    from . import alerts
    alerts.emit(conn, "risk", "Kill switch ON: new entries stopped" if on else "Kill switch off",
                reason or "", "warn", None, f"kill:{ks['at']}")
    conn.execute("INSERT INTO risk_overrides(client_order_id,symbol,check_id,observed,threshold,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                 ("-", "-", "kill_switch", "on" if on else "off", "-", reason or "kill switch turned off", ks["at"]))
    return ks


def override_log(conn, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM risk_overrides ORDER BY id DESC LIMIT ?", (limit,))]


# ---------- portfolio state ----------

def sector_of(conn, symbol: str) -> str | None:
    r = conn.execute("SELECT sector FROM sectors WHERE symbol=?", (symbol,)).fetchone()
    if r and r["sector"]:
        return r["sector"]
    return DEMO_SECTORS.get(symbol) if db.data_mode() == "demo" else None


def open_book(conn, broker: str) -> list[dict]:
    """Open positions and working entries for this broker, with their open risk and market value."""
    from .brokers import simulator as sim
    from .trading import _last_close
    out = []
    for r in conn.execute("""SELECT ord.*, j.plan_json FROM orders ord JOIN journal j ON j.order_id=ord.id
                             JOIN journal_outcomes o ON o.journal_id=j.id WHERE o.status='open' AND ord.broker=?""", (broker,)).fetchall():
        plan = json.loads(r["plan_json"])
        held = sim._open_qty(conn, r["id"])
        working = (r["qty"] - r["filled_qty"]) if r["status"] in ("pending", "partially_filled") else 0
        qty = held + working
        if qty <= 0:
            continue
        entry = r["avg_fill_price"] or plan["entry_assumption"]
        last = _last_close(conn, r["symbol"]) or entry
        out.append({"symbol": r["symbol"], "held": held, "working": working, "qty": qty, "entry": entry, "stop": plan["stop"],
                    "risk": max(0.0, (entry - plan["stop"]) * qty), "value": last * qty, "open_pnl": (last - entry) * held,
                    "sector": sector_of(conn, r["symbol"])})
    return out


def realized_today(conn, session: str | None, broker: str) -> float:
    if not session:
        return 0.0
    r = conn.execute("""SELECT COALESCE(SUM(o.realized_pnl),0) FROM journal_outcomes o JOIN journal j ON j.id=o.journal_id
                        JOIN orders ord ON ord.id=j.order_id WHERE o.status='closed' AND o.exit_date=? AND ord.broker=?""",
                     (session, broker)).fetchone()
    return float(r[0] or 0)


def day_trades_last5(conn, session: str | None) -> int:
    """Same-symbol buy and sell on the same session, over the last five sessions (local fills)."""
    if not session:
        return 0
    rows = conn.execute("""SELECT f.session_date d, o.symbol s, o.side side FROM fills f JOIN orders o ON o.id=f.order_id
                           WHERE f.session_date<=? ORDER BY f.session_date DESC""", (session,)).fetchall()
    days = sorted({r["d"] for r in rows}, reverse=True)[:5]
    n = 0
    for d in days:
        syms = {}
        for r in rows:
            if r["d"] == d:
                syms.setdefault(r["s"], set()).add(r["side"])
        n += sum(1 for v in syms.values() if v == {"buy", "sell"})
    return n


# ---------- the checks ----------

def evaluate(conn, *, symbol: str, qty: int, entry: float, stop: float, equity: float, buying_power: float,
             broker: str, broker_account: dict | None, session: str | None, style: str = "swing") -> dict:
    lim = limits(conn)
    p = lim["params"]
    book = open_book(conn, broker)
    value, new_risk = qty * entry, max(0.0, qty * (entry - stop))
    checks = []

    def add(cid, name, kind, threshold, observed, failed, detail="", unknown=False):
        res = "info" if unknown else ("pass" if not failed else ("block" if kind == "hard" else "override"))
        checks.append({"id": cid, "name": name, "kind": kind, "threshold": threshold, "observed": observed,
                       "result": res, "detail": detail})

    ks = kill_switch(conn)
    add("kill_switch", "Trading kill switch", "hard", "off", "ON" if ks.get("on") else "off", bool(ks.get("on")),
        f"turned on {ks.get('at', '')}: {ks.get('reason') or ''}".strip() if ks.get("on") else "")
    if broker_account is not None:
        blocked = broker_account.get("trading_blocked") or broker_account.get("account_blocked") or broker_account.get("status") not in (None, "ACTIVE")
        add("account_status", "Broker account can trade", "hard", "ACTIVE, not blocked",
            f"{broker_account.get('status')}{', blocked' if blocked else ''}", bool(blocked))
    add("restricted", "Symbol not on restricted list", "hard", f"{len(p['restricted'])} restricted",
        "restricted" if symbol in p["restricted"] else "allowed", symbol in p["restricted"])
    add("buying_power", "Enough buying power", "hard", f"<= ${buying_power:,.0f}", f"${value:,.0f}", value > buying_power)
    day_pnl = realized_today(conn, session, broker) + sum(b["open_pnl"] for b in book)
    loss_pct = -day_pnl / equity * 100 if equity else 0
    add("daily_loss", "Daily loss limit", "hard", f"loss < {p['daily_loss_limit_pct']:g}% of equity",
        f"{'-' if day_pnl < 0 else '+'}${abs(day_pnl):,.0f} ({-loss_pct:+.2f}%)", loss_pct >= p["daily_loss_limit_pct"],
        "realized on the latest session plus open P&L; new entries stop until it recovers or the limit is changed")

    n_open = len({b["symbol"] for b in book} | {symbol})
    add("max_positions", "Open positions", "soft", f"<= {p['max_open_positions']}", f"{n_open} incl. this one",
        n_open > p["max_open_positions"])
    heat = (sum(b["risk"] for b in book) + new_risk) / equity * 100 if equity else 0
    add("portfolio_heat", "Total open risk (portfolio heat)", "soft", f"<= {p['max_portfolio_heat_pct']:g}% of equity",
        f"{heat:.2f}% incl. this one", heat > p["max_portfolio_heat_pct"], "sum of (entry - stop) x shares across open positions")
    sec = sector_of(conn, symbol)
    if sec:
        sec_val = sum(b["value"] for b in book if b["sector"] == sec) + value
        pct = sec_val / equity * 100 if equity else 0
        add("sector", f"Sector concentration ({sec})", "soft", f"<= {p['max_sector_pct']:g}% of equity", f"{pct:.1f}% incl. this one",
            pct > p["max_sector_pct"])
    else:
        add("sector", "Sector concentration", "soft", f"<= {p['max_sector_pct']:g}% of equity", "sector unknown", False,
            "no sector data for this symbol yet", unknown=True)
    dt = broker_account.get("daytrade_count") if broker_account else None
    if dt is None:   # broker doesn't report it: count from local fills
        dt = day_trades_last5(conn, session)
    small = equity < p["pdt_equity_min"]
    if style == "intraday":
        # A day trade under $25k equity with 3 day trades already in 5 sessions would flag the account: hard block.
        add("pdt", "Pattern-day-trader rule", "hard", f"< 3 day trades in 5 sessions when equity < ${p['pdt_equity_min']:,.0f}",
            f"{dt} day trades, equity ${equity:,.0f}", small and dt >= 3,
            "this trade closes the same day, so it counts as a day trade; a 4th in 5 sessions flags the account")
    else:
        add("pdt", "Pattern-day-trader count", "soft", f"< 3 day trades in 5 sessions when equity < ${p['pdt_equity_min']:,.0f}",
            f"{dt} day trades, equity ${equity:,.0f}", small and dt >= 3,
            "a 4th day trade in 5 sessions flags the account as a pattern day trader")
    hard = [c for c in checks if c["result"] == "block"]
    soft = [c for c in checks if c["result"] == "override"]
    return {"limits_version": lim["version"], "checks": checks, "blocked": bool(hard),
            "needs_override": [c["id"] for c in soft], "evaluated_at": _now()}


def enforce(result: dict, overrides: dict | None) -> list[dict]:
    """Raise on hard blocks or missing override reasons; return the overrides to log."""
    from .trading import TradeError
    if result["blocked"]:
        names = ", ".join(c["name"] for c in result["checks"] if c["result"] == "block")
        raise TradeError(f"Blocked by risk checks: {names}.", 422, {"risk": result})
    overrides = overrides or {}
    used, missing = [], []
    for c in result["checks"]:
        if c["result"] != "override":
            continue
        reason = (overrides.get(c["id"]) or "").strip()
        if len(reason) < MIN_REASON:
            missing.append(c["name"])
        else:
            used.append({**c, "reason": reason})
    if missing:
        raise TradeError(f"Override reason needed (at least {MIN_REASON} characters) for: {', '.join(missing)}.", 422, {"risk": result})
    return used


def log_overrides(conn, client_order_id: str, symbol: str, used: list[dict]) -> None:
    for c in used:
        conn.execute("INSERT INTO risk_overrides(client_order_id,symbol,check_id,observed,threshold,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                     (client_order_id, symbol, c["id"], c["observed"], c["threshold"], c["reason"], _now()))
