"""Portfolio dashboard: total balance, allocation, positions with cost and unrealized P&L, and an equity history.

The trading account is the paper (or live) desk; linked accounts (crypto exchanges, wallets, moomoo) add their last read-only
snapshot, valued in USD where a price was available. Equity is sampled at most every 15 minutes whenever the dashboard asks
for it, so the graph fills in as the desk is used; nothing is back-filled or invented.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db, pnl, trading

SAMPLE_EVERY = timedelta(minutes=15)
SCHEMA = """CREATE TABLE IF NOT EXISTS equity_history (ts TEXT PRIMARY KEY, equity REAL NOT NULL, cash REAL NOT NULL,
            invested REAL NOT NULL, linked_usd REAL NOT NULL DEFAULT 0)"""
NOW = None   # tests pin the clock


def _now() -> datetime:
    return NOW() if NOW else datetime.now(timezone.utc)


def _linked() -> dict:
    from . import linked
    try:
        return linked.summary()
    except Exception:
        return {"accounts": [], "total_usd_valued": 0.0}


def sample(conn, acct: dict, linked_usd: float) -> None:
    conn.execute(SCHEMA)
    last = conn.execute("SELECT ts FROM equity_history ORDER BY ts DESC LIMIT 1").fetchone()
    now = _now()
    if last and now - datetime.fromisoformat(last["ts"]) < SAMPLE_EVERY:
        return
    invested = round(sum(p["market_value"] for p in acct["positions"]), 2)
    conn.execute("INSERT OR REPLACE INTO equity_history VALUES (?,?,?,?,?)",
                 (now.isoformat(timespec="seconds"), acct["equity"], acct["cash"], invested, round(linked_usd, 2)))


def _costs(conn) -> dict:
    """Average cost per open simulator position, from its fills (buys add, sells reduce at the running average)."""
    out: dict[str, list[float]] = {}
    for r in conn.execute("""SELECT o.symbol, o.side, f.qty, f.price FROM fills f JOIN orders o ON o.id=f.order_id
                             WHERE o.broker='simulator' ORDER BY f.rowid"""):
        q, cost = out.get(r["symbol"], [0.0, 0.0])
        if r["side"] == "buy":
            q, cost = q + r["qty"], cost + r["qty"] * r["price"]
        else:
            avg = cost / q if q else 0.0
            q, cost = q - r["qty"], cost - r["qty"] * avg
        out[r["symbol"]] = [q, cost]
    return {s: (c / q if q else None) for s, (q, c) in out.items()}


def overview() -> dict:
    lk = _linked()
    linked_usd = float(lk.get("total_usd_valued") or 0)
    with db.tx() as c:
        acct = trading.account(c)
        sample(c, acct, linked_usd)
        costs = _costs(c)
        hist = [dict(r) for r in c.execute("SELECT * FROM equity_history ORDER BY ts")]
        sectors = {r["symbol"]: r["sector"] for r in c.execute("SELECT symbol, sector FROM sectors")}
        realized = pnl.stats(c).get("all")
    positions = []
    for p in acct["positions"]:
        avg = costs.get(p["symbol"])
        positions.append({**p, "avg_cost": round(avg, 4) if avg else None,
                          "unrealized": round((p["last"] - avg) * p["qty"], 2) if avg else None,
                          "unrealized_pct": round((p["last"] / avg - 1) * 100, 2) if avg else None, "sector": sectors.get(p["symbol"])})
    total = round(acct["equity"] + linked_usd, 2)
    alloc = [{"name": "Cash", "kind": "cash", "usd": acct["cash"]}]
    alloc += [{"name": p["symbol"], "kind": "stock", "usd": p["market_value"]} for p in positions]
    alloc += [{"name": a.get("label") or a.get("provider"), "kind": "linked", "usd": (a.get("snapshot") or {}).get("total_usd") or 0}
              for a in lk.get("accounts", []) if (a.get("snapshot") or {}).get("total_usd")]
    for a in alloc:
        a["pct"] = round(a["usd"] / total * 100, 2) if total else 0
    change = round(acct["equity"] - acct["starting_equity"], 2)
    return {"total_usd": total, "trading": acct, "linked_usd": round(linked_usd, 2), "change": change,
            "change_pct": round(change / acct["starting_equity"] * 100, 2) if acct["starting_equity"] else None,
            "positions": positions, "allocation": [a for a in alloc if a["usd"]], "history": hist,
            "workspace": "trading" if db.environment() == "real" else ("test" if db.data_mode() == "live" else "backtest"),
            "realized": realized, "sampled_every_minutes": int(SAMPLE_EVERY.total_seconds() // 60),
            "note": "Equity is sampled while the desk is in use (at most every 15 minutes); linked accounts use their last read-only snapshot."}
