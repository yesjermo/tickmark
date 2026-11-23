"""Reproducible SIMULATED market data for the demo. Not real prices.

Each ticker gets a seeded random walk. The last few visible sessions are shaped
so the watchlist shows four distinct teaching cases as of AS_OF:

  NVDA  qualifying setup  (touched 9-EMA, closed above, volume ~62% of 20-day avg)
  SNDK  near miss         (same shape, but volume ~86% of avg -> fails 'lighter')
        + scheduled earnings inside the 5-session holding window
  IREN  missing info      (price/volume pass, next earnings date unknown)
  COIN  no setup          (closed below the EMA on heavy volume)

FORWARD_SESSIONS more bars exist after AS_OF but stay hidden until the user
advances the simulator clock, so the app never sees "future" data early.
"""
from __future__ import annotations

import math
import random
from datetime import date

from .indicators import ema, trailing_mean
from .tradingcal import next_sessions, sessions_between

SOURCE = "SIMULATED demo generator v1 (seed 20261120) - not real market data"
START = date(2025, 10, 1)
AS_OF = date(2026, 11, 20)
FORWARD_SESSIONS = 12

TICKERS = {
    #        start price, daily drift, daily vol, avg volume, seed
    "NVDA": (118.0, 0.0011, 0.021, 210e6, 11),
    "SNDK": (46.0, 0.0016, 0.030, 9e6, 22),
    "COIN": (240.0, 0.0006, 0.034, 11e6, 33),
    "IREN": (9.5, 0.0019, 0.045, 28e6, 44),
}

# Final-session shape per case: volume multiple of 20-day average, close position vs EMA.
CASES = {
    "NVDA": {"kind": "setup", "vol_mult": 0.62},
    "SNDK": {"kind": "setup", "vol_mult": 0.86},
    "IREN": {"kind": "setup", "vol_mult": 0.70},
    "COIN": {"kind": "breakdown", "vol_mult": 1.30},
}

# Forward drift after AS_OF (what "happens next" in the simulator).
FORWARD = {"NVDA": (0.006, 0.014), "SNDK": (-0.004, 0.025), "COIN": (-0.002, 0.03), "IREN": (0.003, 0.04)}

EARNINGS = {
    "NVDA": (["2025-11-19", "2026-02-25", "2026-05-27", "2026-08-26", "2026-11-18"], "2027-02-24"),
    "SNDK": (["2026-02-05", "2026-05-07", "2026-08-13"], "2026-11-24"),
    "COIN": (["2025-10-30", "2026-02-12", "2026-05-07", "2026-07-30", "2026-10-29"], "2027-02-11"),
    "IREN": (["2025-11-06", "2026-02-12", "2026-05-14", "2026-08-27"], None),  # next date unknown
}


def _walk(rng, n, p0, drift, vol, avg_vol):
    bars, close = [], p0
    for _ in range(n):
        ret = drift + vol * rng.gauss(0, 1)
        open_ = close * (1 + 0.35 * vol * rng.gauss(0, 1))
        new_close = close * math.exp(ret)
        hi = max(open_, new_close) * (1 + abs(rng.gauss(0, vol * 0.5)))
        lo = min(open_, new_close) * (1 - abs(rng.gauss(0, vol * 0.5)))
        v = avg_vol * math.exp(rng.gauss(0, 0.25)) * (1 + 3 * abs(ret))
        bars.append([open_, hi, lo, new_close, v])
        close = new_close
    return bars


def _shape_tail(bars, kind, vol_mult, period=9):
    """Shape the last 4 visible bars into a pullback (or a breakdown) toward the EMA."""
    n = len(bars)
    for k in (4, 3, 2):  # three lighter-volume pullback days
        i = n - k
        closes = [b[3] for b in bars[:i]]
        e_prev = ema(closes, period)[-1]
        prev_close = bars[i - 1][3]
        c = e_prev + (prev_close - e_prev) * 0.55
        o = prev_close * 0.998
        avgv = trailing_mean([b[4] for b in bars], i, 20)
        bars[i] = [o, max(o, c) * 1.006, min(o, c) * 0.994, c, avgv * (0.92 - 0.05 * (4 - k))]
    i = n - 1
    closes = [b[3] for b in bars[:i]]
    e_prev = ema(closes, period)[-1]
    avgv = trailing_mean([b[4] for b in bars], i, 20)
    if kind == "setup":
        o, lo, c = e_prev * 1.011, e_prev * 0.993, e_prev * 1.007
    else:
        o, lo, c = e_prev * 0.992, e_prev * 0.955, e_prev * 0.962
    bars[i] = [o, max(o, c) * 1.007, lo, c, avgv * vol_mult]


def generate() -> tuple[list[dict], list[dict]]:
    visible = sessions_between(START, AS_OF)
    forward = next_sessions(AS_OF, FORWARD_SESSIONS)
    rows = []
    for sym, (p0, drift, vol, avg_vol, seed) in TICKERS.items():
        rng = random.Random(seed * 1000 + 20261120)
        bars = _walk(rng, len(visible), p0, drift, vol, avg_vol)
        case = CASES[sym]
        _shape_tail(bars, case["kind"], case["vol_mult"])
        fdrift, fvol = FORWARD[sym]
        fwd = _walk(random.Random(seed * 7 + 1), len(forward), bars[-1][3], fdrift, fvol, avg_vol * 0.9)
        for d, b in zip(visible + forward, bars + fwd):
            rows.append({"symbol": sym, "date": d.isoformat(),
                         "open": round(b[0], 2), "high": round(b[1], 2), "low": round(b[2], 2),
                         "close": round(b[3], 2), "volume": round(b[4]), "source": SOURCE})
    earnings = []
    for sym, (past, nxt) in EARNINGS.items():
        for d in past:
            earnings.append({"symbol": sym, "date": d, "kind": "past", "source": SOURCE})
        earnings.append({"symbol": sym, "date": nxt, "kind": "next", "source": SOURCE})
    return rows, earnings


def seed(conn, reset: bool = False) -> None:
    from .db import kv_get, kv_set
    if reset:
        for t in ("bars", "earnings", "kv", "rulesets", "previews", "orders", "fills", "journal_outcomes", "forward_signals"):
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DROP TRIGGER IF EXISTS journal_immutable")
        conn.execute("DELETE FROM journal")
        # A demo reset wipes the demo's own audit tables too (the live database keeps its override log on reset).
        conn.execute("DROP TRIGGER IF EXISTS risk_overrides_immutable")
        conn.execute("DROP TRIGGER IF EXISTS risk_overrides_nodelete")
        conn.execute("DELETE FROM risk_overrides")
        conn.execute("DELETE FROM risk_limits")
        from .db import SCHEMA
        conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0]:
        return
    rows, earnings = generate()
    conn.executemany("INSERT INTO bars VALUES (:symbol,:date,:open,:high,:low,:close,:volume,:source)", rows)
    conn.executemany("INSERT INTO earnings VALUES (:symbol,:date,:kind,:source)", earnings)
    if kv_get(conn, "as_of") is None:
        kv_set(conn, "as_of", AS_OF.isoformat())
    kv_set(conn, "watchlist", list(TICKERS))
    kv_set(conn, "account", {"starting_equity": 100000.0, "cash": 100000.0})
