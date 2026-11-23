"""Live market data: real completed daily bars and earnings dates from Yahoo Finance via yfinance.

yfinance is an unofficial client (not affiliated with Yahoo). Data can be delayed,
revised or rate-limited, so:
  * only COMPLETED sessions are stored - today's bar is dropped until 16:15 ET;
  * a failed fetch keeps the last good cached bars and reports the error;
  * future earnings dates from Yahoo can be estimates; they are labelled as such.

After new sessions arrive, the paper simulator runs each one in order, so paper
orders fill at the REAL next open, stops trigger on REAL lows, and planned exits
use REAL closes.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from . import db
from .tradingcal import is_session

SOURCE = "Yahoo Finance via yfinance (unofficial, research use; completed daily bars)"
ET = ZoneInfo("America/New_York")
SESSION_DONE = time(16, 15)
HISTORY_PERIOD = "2y"
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

_yf = None  # injectable for tests


def yf():
    global _yf
    if _yf is None:
        import yfinance
        _yf = yfinance
    return _yf


def now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(ET)


def last_completed_session(now: datetime | None = None) -> date:
    """Latest session whose regular-hours bar is final."""
    now = now or now_et()
    d = now.date()
    if is_session(d) and now.time() >= SESSION_DONE:
        return d
    from datetime import timedelta
    d -= timedelta(days=1)
    while not is_session(d):
        d -= timedelta(days=1)
    return d


def fetch_bars(symbol: str, now: datetime | None = None) -> list[dict]:
    df = yf().Ticker(symbol).history(period=HISTORY_PERIOD, interval="1d", auto_adjust=False)
    if df is None or df.empty:
        raise ValueError(f"no price history returned for {symbol}")
    cutoff = last_completed_session(now).isoformat()
    out = []
    for ts, r in df.iterrows():
        d = ts.date().isoformat()
        if d > cutoff:
            continue  # session still in progress (or clock skew): never store a partial bar
        vals = [r.get("Open"), r.get("High"), r.get("Low"), r.get("Close"), r.get("Volume")]
        clean = [None if v is None or v != v else float(v) for v in vals]  # NaN -> None (missing, not zero)
        out.append({"symbol": symbol, "date": d, "open": clean[0], "high": clean[1], "low": clean[2],
                    "close": clean[3], "volume": clean[4], "source": SOURCE})
    if not out:
        raise ValueError(f"no completed sessions for {symbol}")
    last = out[-1]
    if last["close"] is None and last["date"] == cutoff:
        # Yahoo sometimes leaves the newest daily close blank (seen for SNDK on 2026-11-20) while its quote already has it.
        # Once the session is complete, the regular-market price is that session's close; label where it came from.
        try:
            fi = yf().Ticker(symbol).fast_info
            px = fi.get("lastPrice") if hasattr(fi, "get") else getattr(fi, "last_price", None)
        except Exception:
            px = None
        if px and px == px and px > 0:
            last["close"] = round(float(px), 4)
            last["source"] = f"{SOURCE} (close filled from the Yahoo quote; history had none)"
    return out


def fetch_earnings(symbol: str, today: date) -> dict:
    """Past and next earnings dates. Next is None (unknown) if Yahoo has no future date."""
    t = yf().Ticker(symbol)
    rows = []
    try:
        ed = t.get_earnings_dates(limit=12)
        if ed is not None and not ed.empty:
            for ts in ed.index:
                local = ts.tz_convert(ET) if ts.tzinfo else ts
                rows.append((local.date().isoformat(), "after close" if local.hour >= 16 else
                             "before open" if local.hour and local.hour < 9 else "time not given"))
    except Exception:
        pass
    if not rows:
        try:
            cal = t.calendar or {}
            for d in cal.get("Earnings Date", []) or []:
                rows.append((d.isoformat(), "time not given"))
        except Exception:
            pass
    rows = sorted(set(rows))
    past = [d for d, _ in rows if d <= today.isoformat()]
    future = [(d, tm) for d, tm in rows if d > today.isoformat()]
    return {"past": past, "next": future[0][0] if future else None, "timing": future[0][1] if future else None}


def validate_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if not SYMBOL_RE.match(s):
        raise ValueError(f"'{symbol}' is not a valid US ticker symbol")
    return s


def fetch_sector(symbol: str) -> str | None:
    try:
        return (yf().Ticker(symbol).info or {}).get("sector") or None
    except Exception:
        return None


def fetch(symbols: list[str], now: datetime | None = None, need_sector: set[str] | None = None,
          split_symbols: list[str] | None = None) -> dict:
    """Network phase: no database access, so slow or failing requests never hold a write lock.

    Bars come from the selected provider (market_data.provider()); earnings dates and sectors
    from Yahoo. Splits are looked up for `split_symbols` (open positions and the watchlist)."""
    from datetime import timedelta
    from . import market_data
    now = now or now_et()
    prov = market_data.provider()
    cutoff = last_completed_session(now).isoformat()
    licensed, note = {}, None
    if prov == "alpaca" and symbols:
        try:
            licensed = market_data.alpaca_bars(symbols, now.date() - timedelta(days=730), cutoff)
        except market_data.SipNotPermitted as exc:
            note = f"Alpaca refused SIP data ({exc}); using Yahoo instead. IEX-only volume would distort volume rules."
        except Exception as exc:
            note = f"Alpaca data failed ({type(exc).__name__}: {exc}); using Yahoo instead."[:300]
    items = {}
    for sym in symbols:
        try:
            if licensed.get(sym):
                bars, used = licensed[sym], "alpaca"
            else:
                bars, used = fetch_bars(sym, now), "yahoo"
            items[sym] = {"bars": bars, "earn": fetch_earnings(sym, now.date()), "provider": used}
            if need_sector is None or sym in need_sector:
                items[sym]["sector"] = fetch_sector(sym)
        except Exception as exc:  # keep cached data, surface the problem
            items[sym] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    splits = []
    try:
        splits = market_data.fetch_splits(sorted(set(split_symbols or symbols)), now.date() - timedelta(days=30), now.date())
    except Exception:
        pass
    return {"now": now, "items": items, "provider": prov, "provider_note": note, "splits": splits}


def store(conn, fetched: dict) -> dict:
    """Write phase: apply splits, upsert fetched bars/earnings, check data quality, record the report."""
    from . import market_data
    now, report, ok_dates = fetched["now"], {}, []
    split_events = market_data.apply_splits(conn, fetched.get("splits") or [])
    for sym, it in fetched["items"].items():
        if "error" in it:
            report[sym] = {"ok": False, "error": it["error"]}
            continue
        bars, earn = it["bars"], it["earn"]
        conn.executemany(
            """INSERT INTO bars VALUES (:symbol,:date,:open,:high,:low,:close,:volume,:source)
               ON CONFLICT(symbol,date) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low,
               close=excluded.close, volume=excluded.volume, source=excluded.source""", bars)
        conn.execute("DELETE FROM earnings WHERE symbol=?", (sym,))
        for d in earn["past"]:
            conn.execute("INSERT INTO earnings VALUES (?,?,?,?)", (sym, d, "past", SOURCE))
        conn.execute("INSERT INTO earnings VALUES (?,?,?,?)",
                     (sym, earn["next"], "next", f"{SOURCE}; next date may be an estimate ({earn['timing'] or 'unknown timing'})"))
        if it.get("sector"):
            conn.execute("INSERT INTO sectors VALUES(?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET sector=excluded.sector, "
                         "source=excluded.source, updated_at=excluded.updated_at",
                         (sym, it["sector"], "Yahoo Finance profile", now.isoformat(timespec="seconds")))
        ok_dates.append(bars[-1]["date"])
        report[sym] = {"ok": True, "last_bar": bars[-1]["date"], "bars": len(bars), "next_earnings": earn["next"],
                       "earnings_timing": earn["timing"], "provider": it.get("provider", "yahoo")}
    fetched_at = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    expected = last_completed_session(now).isoformat()
    quality = market_data.check_quality(conn, [s for s in fetched["items"]], expected)
    market_data.record_quality(conn, quality, fetched.get("provider", "yahoo"))
    db.kv_set(conn, "live_fetch", {"fetched_at": fetched_at, "report": report, "expected_last_session": expected,
                                   "provider": fetched.get("provider", "yahoo"), "provider_note": fetched.get("provider_note")})
    return {"fetched_at": fetched_at, "report": report, "latest": max(ok_dates) if ok_dates else None,
            "provider": fetched.get("provider", "yahoo"), "provider_note": fetched.get("provider_note"),
            "splits": split_events, "quality": {s: q["ok"] for s, q in quality.items()}}


def refresh(conn, symbols: list[str], now: datetime | None = None) -> dict:
    return store(conn, fetch(symbols, now))
