"""Journal entries for crypto trades and P&L statistics across stocks and crypto.

Crypto P&L is computed from the exchange's own fills in crypto_orders:
  realized = sum(exit filled x exit average) - entry average x closed amount - fees in the quote currency
An entry (a buy placed through Tickmark) is closed once its stop or close order has filled the bought amount.
Stablecoin quotes (USDT, USDC) are counted as dollars; other quotes are reported but not added to the dollar totals.
"""
from __future__ import annotations

import json

USD_LIKE = {"USD", "USDT", "USDC", "FDUSD", "DAI", "PYUSD", "USDG"}


def _fee_in_quote(o: dict, quote: str) -> float:
    try:
        f = json.loads(o.get("fee") or "null")
    except ValueError:
        return 0.0
    if isinstance(f, dict) and f.get("currency") == quote:
        return float(f.get("cost") or 0)
    return 0.0


def crypto_journal(conn) -> list[dict]:
    out = []
    entries = [dict(r) for r in conn.execute(
        "SELECT * FROM crypto_orders WHERE parent_id IS NULL AND purpose='entry' AND side='buy' AND filled > 0 ORDER BY created_at DESC")]
    for e in entries:
        kids = [dict(r) for r in conn.execute("SELECT * FROM crypto_orders WHERE parent_id=? ORDER BY created_at", (e["id"],))]
        pv = json.loads(e.get("preview_json") or "{}")
        quote = pv.get("quote") or e["symbol"].split("/")[-1]
        exits = [k for k in kids if k["purpose"] in ("stop", "exit") and (k["filled"] or 0) > 0]
        sold = sum(k["filled"] for k in exits)
        proceeds = sum(k["filled"] * (k["average"] or 0) for k in exits)
        fees = _fee_in_quote(e, quote) + sum(_fee_in_quote(k, quote) for k in exits)
        closed_amt = min(sold, e["filled"])
        basis = (e["average"] or 0) * closed_amt
        closed = sold >= e["filled"] * 0.999
        realized = round(proceeds - basis - fees, 2) if exits else None
        stop = next((k for k in kids if k["purpose"] == "stop"), None)
        reason = None
        if closed:
            reason = "stop" if any(k["purpose"] == "stop" for k in exits) else "manual_exit"
        out.append({
            "id": e["id"], "asset_class": "crypto", "symbol": e["symbol"], "quote": quote, "usd_like": quote in USD_LIKE,
            "venue": pv.get("account_label") or e["provider"], "environment": e["environment"], "created_at": e["created_at"],
            "thesis": e.get("thesis"), "status": "closed" if closed else "open", "amount": e["filled"], "entry_price": e["average"],
            "exit_price": round(proceeds / sold, 8) if sold else None, "exit_reason": reason, "fees": round(fees, 4),
            "realized_pnl": realized if closed else None,
            "return_pct": round(realized / basis * 100, 2) if closed and basis else None,
            "plan": {"stop_price": e["stop_price"]},
            "closed_at": max((k["updated_at"] for k in exits), default=None) if closed else None,
            "adherence": {"followed": bool(stop and stop["status"] != "rejected") if e["stop_price"] else False,
                          "checks": [{"check": "Protective stop at the exchange", "ok": bool(stop and stop["status"] != "rejected"),
                                      "detail": ("none planned" if not e["stop_price"] else
                                                 f"stop {stop['status']} at {stop['stop_price']}" if stop else "not placed yet")}]},
        })
    return out


def _summ(rows: list[dict]) -> dict:
    closed = [r for r in rows if r["status"] == "closed" and r.get("realized_pnl") is not None]
    wins = [r["realized_pnl"] for r in closed if r["realized_pnl"] > 0]
    losses = [r["realized_pnl"] for r in closed if r["realized_pnl"] <= 0]
    gross_w, gross_l = sum(wins), -sum(losses)
    rets = [r["return_pct"] for r in closed if r.get("return_pct") is not None]
    followed = [r for r in closed if (r.get("adherence") or {}).get("followed")]
    return {
        "trades": len(rows), "open": sum(1 for r in rows if r["status"] == "open"), "closed": len(closed),
        "realized_pnl": round(sum(r["realized_pnl"] for r in closed), 2),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_win": round(gross_w / len(wins), 2) if wins else None, "avg_loss": round(-gross_l / len(losses), 2) if losses else None,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
        "avg_return_pct": round(sum(rets) / len(rets), 2) if rets else None,
        "best": max((r["realized_pnl"] for r in closed), default=None), "worst": min((r["realized_pnl"] for r in closed), default=None),
        "plan_followed_pct": round(len(followed) / len(closed) * 100, 1) if closed else None,
    }


def stats(conn) -> dict:
    from .trading import journal
    stocks = []
    for j in journal(conn):
        plan = j.get("plan") or {}
        stocks.append({"asset_class": "stock", "symbol": j["symbol"], "status": j["status"], "realized_pnl": j.get("realized_pnl"),
                       "return_pct": j.get("return_pct"), "adherence": j.get("adherence"),
                       "style": plan.get("style", "swing"), "venue": (j.get("order_detail") or {}).get("broker", "simulator")})
    crypto = [c for c in crypto_journal(conn) if c["usd_like"]]
    other_quote = [c for c in crypto_journal(conn) if not c["usd_like"]]
    swing = [s for s in stocks if s["style"] != "intraday"]
    day = [s for s in stocks if s["style"] == "intraday"]
    by_venue: dict[str, list] = {}
    for r in stocks + crypto:
        by_venue.setdefault(r["venue"], []).append(r)
    return {"all": _summ(stocks + crypto), "stocks_swing": _summ(swing), "stocks_day": _summ(day), "crypto": _summ(crypto),
            "by_venue": {k: _summ(v) for k, v in sorted(by_venue.items())},
            "excluded_non_usd_crypto": len(other_quote),
            "note": "Realized P&L from fills, after fees the exchange reported in the quote currency. Crypto quoted in USDT/USDC counts as USD."}
