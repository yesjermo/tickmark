"""Market data providers, stock splits and data-quality checks.

Providers for completed daily bars (setting "data_provider"):
  yahoo   free, unofficial (yfinance), research use
  alpaca  licensed: Alpaca Market Data API, SIP consolidated tape, split-adjusted.
          The free Alpaca plan allows SIP history for completed sessions (checked
          2026-11-22 with a paper account); IEX-only volume is ~3% of the consolidated
          total and would distort volume rules, so Tickmark insists on SIP and falls
          back to Yahoo (with a note) if SIP is refused.
Earnings dates and sectors still come from Yahoo for both, because Alpaca has no
earnings calendar; the source label says so.

Splits: Alpaca corporate actions (GET /v1/corporate-actions) when Alpaca is connected,
otherwise Yahoo's split history. When a split's ex-date passes for a symbol with open
positions or working orders, local fills, orders, stops and forward-test levels are
rescaled so they line up with the split-adjusted bars and with the broker's adjusted
position. The journal's entry snapshot is never edited.

Data quality: after each refresh every symbol is checked for missing sessions,
impossible bars (high < low, close outside the range), missing prices, zero volume and
unexplained one-day jumps. Issues on the latest bar make the screener's data rule
"unknown", so a bad print can't produce a signal.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import httpx

from . import credentials, db
from .tradingcal import sessions_between

DATA_URL = "https://data.alpaca.markets"
TRANSPORT: httpx.BaseTransport | None = None   # injectable for tests
ALPACA_SOURCE = "Alpaca Market Data (SIP consolidated tape, split-adjusted; completed daily bars; earnings dates from Yahoo)"
JUMP_LIMIT = 0.30
QUALITY_WINDOW = 60

PROVIDERS = {
    "yahoo": {"name": "Yahoo Finance (free, unofficial)", "licensed": False},
    "alpaca": {"name": "Alpaca SIP (licensed)", "licensed": True},
}


def provider() -> str:
    p = db.get_setting("data_provider", "yahoo")
    return p if p in PROVIDERS else "yahoo"


def set_provider(p: str) -> None:
    if p not in PROVIDERS:
        raise ValueError("data provider must be yahoo or alpaca")
    if p == "alpaca" and not credentials.get_alpaca(db.environment())[0]:
        raise ValueError("Connect Alpaca in the Broker tab first: its market data uses the same keys.")
    db.set_setting("data_provider", p)


def _client() -> httpx.Client:
    k, s, _ = credentials.get_alpaca(db.environment())
    if not (k and s):
        raise RuntimeError("Alpaca keys not connected")
    return httpx.Client(base_url=DATA_URL, transport=TRANSPORT, timeout=20,
                        headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s})


class SipNotPermitted(Exception):
    pass


def _et_date(ts: str) -> str:
    from .live_data import ET
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET).date().isoformat()


def alpaca_bars(symbols: list[str], start: date, cutoff: str) -> dict[str, list[dict]]:
    """Completed daily bars (SIP, split-adjusted) for many symbols, following pagination."""
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start.isoformat(), "adjustment": "split",
              "feed": "sip", "limit": 10000, "sort": "asc"}
    with _client() as c:
        for _ in range(50):
            r = c.get("/v2/stocks/bars", params=params)
            if r.status_code == 403:
                raise SipNotPermitted(r.text[:200])
            if r.status_code >= 400:
                raise RuntimeError(f"Alpaca data error {r.status_code}: {r.text[:200]}")
            d = r.json()
            for sym, bars in (d.get("bars") or {}).items():
                for b in bars:
                    day = _et_date(b["t"])
                    if day > cutoff:
                        continue
                    out.setdefault(sym, []).append({"symbol": sym, "date": day, "open": b.get("o"), "high": b.get("h"),
                                                    "low": b.get("l"), "close": b.get("c"), "volume": b.get("v"),
                                                    "source": ALPACA_SOURCE})
            tok = d.get("next_page_token")
            if not tok:
                break
            params["page_token"] = tok
    return out


# ---------- splits ----------

def alpaca_splits(symbols: list[str], start: date, end: date) -> list[dict]:
    out = []
    with _client() as c:
        params = {"symbols": ",".join(symbols), "types": "forward_split,reverse_split", "start": start.isoformat(),
                  "end": end.isoformat(), "limit": 1000}
        for _ in range(20):
            r = c.get("/v1/corporate-actions", params=params)
            if r.status_code >= 400:
                raise RuntimeError(f"Alpaca corporate actions error {r.status_code}: {r.text[:200]}")
            d = r.json()
            ca = d.get("corporate_actions") or {}
            for kind in ("forward_splits", "reverse_splits"):
                for x in ca.get(kind, []) or []:
                    old, new = float(x["old_rate"]), float(x["new_rate"])
                    out.append({"id": f"alpaca:{x['id']}", "symbol": x["symbol"], "ex_date": x["ex_date"],
                                "ratio": new / old, "label": f"{new:g}-for-{old:g}", "source": "Alpaca corporate actions"})
            if not d.get("next_page_token"):
                break
            params["page_token"] = d["next_page_token"]
    return out


def yahoo_splits(symbols: list[str], start: date, end: date) -> list[dict]:
    from .live_data import yf
    out = []
    for sym in symbols:
        try:
            ser = yf().Ticker(sym).splits
        except Exception:
            continue
        if ser is None or len(ser) == 0:
            continue
        for ts, ratio in ser.items():
            d = ts.date() if hasattr(ts, "date") else ts
            if start <= d <= end and ratio and float(ratio) != 1.0:
                out.append({"id": f"yahoo:{sym}:{d.isoformat()}", "symbol": sym, "ex_date": d.isoformat(), "ratio": float(ratio),
                            "label": f"{float(ratio):g}-for-1" if ratio >= 1 else f"1-for-{1 / float(ratio):g}",
                            "source": "Yahoo split history"})
    return out


def fetch_splits(symbols: list[str], start: date, end: date) -> list[dict]:
    if credentials.get_alpaca(db.environment())[0]:
        try:
            return alpaca_splits(symbols, start, end)
        except Exception:
            pass
    return yahoo_splits(symbols, start, end)


def apply_splits(conn, splits: list[dict]) -> list[str]:
    """Rescale open positions, working orders and forward-test levels for splits not yet applied."""
    from . import alerts
    events = []
    for sp in splits:
        if conn.execute("SELECT 1 FROM corporate_actions WHERE id=?", (sp["id"],)).fetchone():
            continue
        r, sym, ex = sp["ratio"], sp["symbol"], sp["ex_date"]
        touched = 0
        entries = conn.execute("""SELECT ord.* FROM orders ord JOIN journal j ON j.order_id=ord.id
                                  JOIN journal_outcomes o ON o.journal_id=j.id
                                  WHERE ord.symbol=? AND o.status='open'""", (sym,)).fetchall()
        for e in entries:
            ids = [e["id"]] + [c["id"] for c in conn.execute("SELECT id FROM orders WHERE parent_id=?", (e["id"],))]
            for oid in ids:
                for f in conn.execute("SELECT * FROM fills WHERE order_id=? AND session_date<?", (oid, ex)).fetchall():
                    conn.execute("UPDATE fills SET qty=?, price=?, note=COALESCE(note,'') || ? WHERE id=?",
                                 (round(f["qty"] * r), round(f["price"] / r, 4), f" [split {sp['label']} {ex}]", f["id"]))
                o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
                conn.execute("""UPDATE orders SET qty=?, filled_qty=?, avg_fill_price=?, stop_price=?, limit_price=?,
                                status_reason=COALESCE(status_reason,'') || ? WHERE id=?""",
                             (round(o["qty"] * r), round((o["filled_qty"] or 0) * r),
                              None if o["avg_fill_price"] is None else round(o["avg_fill_price"] / r, 4),
                              None if o["stop_price"] is None else round(o["stop_price"] / r, 4),
                              None if o["limit_price"] is None else round(o["limit_price"] / r, 4),
                              f" [adjusted for {sp['label']} split]", oid))
            touched += 1
        for fs in conn.execute("SELECT * FROM forward_signals WHERE symbol=? AND state!='closed' AND signal_date<?", (sym, ex)).fetchall():
            conn.execute("UPDATE forward_signals SET stop=?, entry_price=? WHERE id=?",
                         (round(fs["stop"] / r, 4), None if fs["entry_price"] is None else round(fs["entry_price"] / r, 4), fs["id"]))
            touched += 1
        conn.execute("INSERT INTO corporate_actions(id,symbol,kind,ratio,ex_date,label,source,applied_at,touched) VALUES(?,?,?,?,?,?,?,?,?)",
                     (sp["id"], sym, "split", r, ex, sp["label"], sp["source"], datetime.now(timezone.utc).isoformat(timespec="seconds"), touched))
        if touched:
            alerts.emit(conn, "data", f"Split: {sym} {sp['label']} (ex-date {ex})",
                        f"Adjusted {touched} open position(s)/signal(s): quantities x{r:g}, prices and stops /{r:g}. "
                        "The journal's entry snapshot keeps the original numbers.", "warn", sym, f"split:{sp['id']}")
            events.append(f"{sym}: {sp['label']} split applied to {touched} open item(s)")
    return events


# ---------- data quality ----------

def check_quality(conn, symbols: list[str], expected_last: str) -> dict:
    out = {}
    split_days = {(r["symbol"], r["ex_date"]) for r in conn.execute("SELECT symbol, ex_date FROM corporate_actions")}
    for sym in symbols:
        rows = conn.execute("SELECT * FROM bars WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT ?",
                            (sym, expected_last, QUALITY_WINDOW)).fetchall()[::-1]
        issues = []
        if not rows:
            out[sym] = {"ok": False, "issues": [{"kind": "no_data", "date": None, "detail": "no bars stored"}], "latest_issue": None}
            continue
        have = {r["date"] for r in rows}
        first = date.fromisoformat(rows[0]["date"])
        missing = [d.isoformat() for d in sessions_between(first, date.fromisoformat(expected_last)) if d.isoformat() not in have]
        for d in missing[-5:]:
            issues.append({"kind": "missing_session", "date": d, "detail": "no bar for this trading session"})
        prev = None
        for r in rows:
            o, h, l, c, v = r["open"], r["high"], r["low"], r["close"], r["volume"]
            if None in (o, h, l, c):
                issues.append({"kind": "missing_price", "date": r["date"], "detail": "open/high/low/close missing"})
            elif h < l or c > h * 1.0005 or c < l * 0.9995 or o > h * 1.0005 or o < l * 0.9995:
                issues.append({"kind": "impossible_bar", "date": r["date"], "detail": f"O {o} H {h} L {l} C {c} is not a valid range"})
            if v is not None and v <= 0:
                issues.append({"kind": "zero_volume", "date": r["date"], "detail": "no volume reported"})
            if prev is not None and prev["close"] and c and (sym, r["date"]) not in split_days:
                jump = c / prev["close"] - 1
                if abs(jump) > JUMP_LIMIT:
                    issues.append({"kind": "unexplained_jump", "date": r["date"], "detail": f"{jump:+.0%} in one session with no split on record"})
            prev = r if r["close"] else prev
        latest = rows[-1]["date"]
        latest_issue = next((i for i in issues if i["date"] == latest and i["kind"] in ("missing_price", "impossible_bar", "zero_volume")), None)
        out[sym] = {"ok": not issues, "issues": issues, "latest_issue": latest_issue}
    return out


def record_quality(conn, report: dict, prov: str) -> None:
    from . import alerts
    db.kv_set(conn, "data_quality", {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "provider": prov, "symbols": report})
    for sym, r in report.items():
        for i in r["issues"]:
            if i["kind"] in ("impossible_bar", "unexplained_jump", "missing_price", "no_data"):
                alerts.emit(conn, "data", f"Data quality: {sym} {i['kind'].replace('_', ' ')}", f"{i['date'] or ''} {i['detail']}".strip(),
                            "warn", sym, f"dq:{prov}:{sym}:{i['kind']}:{i['date']}")
