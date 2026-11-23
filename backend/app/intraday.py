"""Day trading: intraday data, indicators, strategies, a session clock and paper execution.

Data: 5-minute regular-session bars (09:30-16:00 ET).
  * Alpaca (when connected): live sessions use the IEX real-time feed (the only real-time feed
    on Alpaca's free plan); replayed past sessions use the SIP consolidated tape. Relative volume
    compares today with past days FROM THE SAME FEED, so IEX's smaller volume doesn't distort it.
  * Yahoo (no keys): 5-minute bars for roughly the last 60 days; delayed during the session.
Only completed bars are used: a bar counts once its end time has passed.

Session clock (kv "day_session"):
  live    today's session, following the wall clock
  replay  a real past session stepped one bar at a time; only bars up to the cursor exist,
          so replay can't use hindsight. Historical quotes aren't available, so spread and
          quote-freshness checks are shown as "not available in replay" and don't count.

Strategies (long only):
  vwap_reclaim  price crosses back above session VWAP on the latest bar, with heavy relative volume
  orb           first close above the opening-range high (default 15 minutes), heavy relative volume
Both: entry window 09:45-15:30, spread and quote-freshness checks when live, stop below the
reclaim bar / VWAP (or the opening-range midpoint), profit target at R multiple, and flat by 15:55.

Paper execution: entry fills at the NEXT bar's open (+slippage); stop and target are OCO; if a
bar touches both, the stop is assumed first (conservative); any position still open is sold at
the close of the 15:55 bar. Every intraday round trip is a day trade for the PDT rule.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone

import httpx

from . import credentials, db
from .brokers import simulator as sim
from .tradingcal import is_session, next_sessions

BAR_MIN = 5
OPEN_T, CLOSE_T = time(9, 30), time(16, 0)
SLIPPAGE_BPS = 2.0
DATA_URL = "https://data.alpaca.markets"
TRANSPORT: httpx.BaseTransport | None = None
FETCH = None        # tests inject: FETCH(symbols, start_day, end_day, feed) -> {sym: [bar, ...]}

DAY_STRATEGIES = {
    "vwap_reclaim": {"name": "VWAP reclaim", "style": "Buy the move back above the day's average price on heavy volume",
                     "core": ("in_window", "vwap_reclaim", "rvol")},
    "orb": {"name": "Opening-range breakout", "style": "Buy the first break above the opening range on heavy volume",
            "core": ("in_window", "or_breakout", "rvol")},
}


@dataclass
class DayParams:
    strategy: str = "vwap_reclaim"
    rvol_min: float = 1.5
    rvol_days: int = 10
    or_minutes: int = 15
    window_start: str = "09:45"
    window_end: str = "15:30"
    flat_by: str = "15:55"
    target_r: float = 2.0
    stop_buffer_pct: float = 0.10
    orb_stop: str = "mid"            # 'mid' (opening-range midpoint) | 'low'
    spread_max_bps: float = 10.0
    quote_max_age_s: float = 15.0
    risk_per_trade_pct: float = 0.25
    max_position_pct: float = 20.0

    @classmethod
    def from_dict(cls, d: dict) -> "DayParams":
        p = cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})
        p.validate()
        return p

    def validate(self):
        def t(x):
            return time.fromisoformat(x)
        checks = [(self.strategy in DAY_STRATEGIES, "unknown day-trading strategy"),
                  (1.0 <= self.rvol_min <= 20, "relative volume must be 1-20"),
                  (3 <= self.rvol_days <= 30, "relative-volume history must be 3-30 days"),
                  (self.or_minutes in (5, 10, 15, 30, 60), "opening range must be 5, 10, 15, 30 or 60 minutes"),
                  (OPEN_T < t(self.window_start) < t(self.window_end) < t(self.flat_by) <= time(15, 55),
                   "times must be in order: open < window start < window end < flat-by <= 15:55"),
                  (0.5 <= self.target_r <= 10, "target must be 0.5-10 R"),
                  (0 <= self.stop_buffer_pct <= 5, "stop buffer must be 0-5%"),
                  (self.orb_stop in ("mid", "low"), "ORB stop must be 'mid' or 'low'"),
                  (1 <= self.spread_max_bps <= 200, "spread limit must be 1-200 bps"),
                  (1 <= self.quote_max_age_s <= 300, "quote age limit must be 1-300 s"),
                  (0.05 <= self.risk_per_trade_pct <= 5, "risk per trade must be 0.05-5%"),
                  (1 <= self.max_position_pct <= 100, "max position must be 1-100%")]
        for ok, msg in checks:
            if not ok:
                raise ValueError(msg)


# ---------- time helpers ----------

def ET():
    from .live_data import ET as _et
    return _et


def now_et() -> datetime:
    from .live_data import now_et as _n
    return _n()


def bar_starts(day: date) -> list[datetime]:
    t = datetime.combine(day, OPEN_T, tzinfo=ET())
    out = []
    while t.time() < CLOSE_T:
        out.append(t)
        t += timedelta(minutes=BAR_MIN)
    return out


def _iso(dt: datetime) -> str:
    return dt.astimezone(ET()).strftime("%Y-%m-%dT%H:%M")


def end_of(ts: str) -> str:
    return _iso(datetime.fromisoformat(ts).replace(tzinfo=ET()) + timedelta(minutes=BAR_MIN))


def prior_sessions(day: date, n: int) -> list[date]:
    out, d = [], day
    while len(out) < n:
        d -= timedelta(days=1)
        if is_session(d):
            out.append(d)
    return out[::-1]


# ---------- data ----------

def feed_for(mode: str) -> str:
    if not credentials.get_alpaca(db.environment())[0]:
        return "yahoo"
    return "alpaca:iex" if mode == "live" else "alpaca:sip"


def _fetch_alpaca(symbols, start_day: date, end_day: date, feed: str) -> dict:
    k, s, _ = credentials.get_alpaca(db.environment())
    start = datetime.combine(start_day, OPEN_T, tzinfo=ET()).astimezone(timezone.utc)
    end = datetime.combine(end_day, CLOSE_T, tzinfo=ET()).astimezone(timezone.utc)
    params = {"symbols": ",".join(symbols), "timeframe": f"{BAR_MIN}Min", "start": start.isoformat().replace("+00:00", "Z"),
              "end": end.isoformat().replace("+00:00", "Z"), "feed": feed.split(":")[1], "limit": 10000, "sort": "asc"}
    out = {sym: [] for sym in symbols}
    with httpx.Client(base_url=DATA_URL, transport=TRANSPORT, timeout=20,
                      headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}) as c:
        for _ in range(50):
            r = c.get("/v2/stocks/bars", params=params)
            if r.status_code >= 400:
                raise RuntimeError(f"Alpaca intraday data error {r.status_code}: {r.text[:200]}")
            d = r.json()
            for sym, bars in (d.get("bars") or {}).items():
                for b in bars:
                    t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET())
                    if OPEN_T <= t.time() < CLOSE_T:
                        out.setdefault(sym, []).append({"ts": _iso(t), "day": t.date().isoformat(), "open": b["o"], "high": b["h"],
                                                        "low": b["l"], "close": b["c"], "volume": b["v"], "source": feed})
            if not d.get("next_page_token"):
                break
            params["page_token"] = d["next_page_token"]
    return out


def _fetch_yahoo(symbols, start_day: date, end_day: date, feed: str) -> dict:
    from .live_data import yf
    out = {}
    for sym in symbols:
        df = yf().Ticker(sym).history(start=start_day.isoformat(), end=(end_day + timedelta(days=1)).isoformat(),
                                      interval=f"{BAR_MIN}m", auto_adjust=False)
        rows = []
        for ts, r in (df.iterrows() if df is not None else []):
            t = ts.tz_convert(ET()) if ts.tzinfo else ts.replace(tzinfo=ET())
            if OPEN_T <= t.time() < CLOSE_T and r.get("Close") == r.get("Close"):
                rows.append({"ts": _iso(t), "day": t.date().isoformat(), "open": float(r["Open"]), "high": float(r["High"]),
                             "low": float(r["Low"]), "close": float(r["Close"]), "volume": float(r["Volume"]), "source": "yahoo"})
        out[sym] = rows
    return out


def fetch(symbols, start_day: date, end_day: date, feed: str) -> dict:
    if FETCH:
        return FETCH(symbols, start_day, end_day, feed)
    return _fetch_alpaca(symbols, start_day, end_day, feed) if feed.startswith("alpaca") else _fetch_yahoo(symbols, start_day, end_day, feed)


def ensure_data(symbols: list[str], day: date, feed: str, lookback: int, refresh_today: bool = False) -> dict:
    """Make sure `day` and the `lookback` sessions before it are cached from `feed`. Network outside any transaction."""
    days = prior_sessions(day, lookback) + [day]
    c = db.connect()
    try:
        need = []
        for d in days:
            iso = d.isoformat()
            for sym in symbols:
                row = c.execute("SELECT COUNT(*), MIN(source) FROM intraday_bars WHERE symbol=? AND day=?", (sym, iso)).fetchone()
                complete = row[0] >= len(bar_starts(d)) - 1 and row[1] == feed
                if not complete or (d == day and refresh_today):
                    need.append(d)
                    break
    finally:
        c.close()
    if not need:
        return {"fetched": 0}
    got = fetch(symbols, min(need), max(need), feed)
    n = 0
    with db.tx() as conn:
        for sym, bars in got.items():
            for b in bars:
                if date.fromisoformat(b["day"]) not in need:
                    continue
                conn.execute("""INSERT INTO intraday_bars VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol, ts) DO UPDATE SET
                                open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                                volume=excluded.volume, source=excluded.source, day=excluded.day""",
                             (sym, b["ts"], b["day"], b["open"], b["high"], b["low"], b["close"], b["volume"], b["source"]))
                n += 1
    return {"fetched": n, "days": [d.isoformat() for d in need]}


def bars_until(conn, sym: str, day: str, cursor: str) -> list[dict]:
    """Bars of `day` whose END time is <= cursor (completed by then)."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM intraday_bars WHERE symbol=? AND day=? ORDER BY ts", (sym, day))]
    return [r for r in rows if end_of(r["ts"]) <= cursor]


def day_bars(conn, sym: str, day: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM intraday_bars WHERE symbol=? AND day=? ORDER BY ts", (sym, day))]


# ---------- indicators ----------

def vwap(bars: list[dict]) -> list[float | None]:
    out, pv, vv = [], 0.0, 0.0
    for b in bars:
        if None in (b["high"], b["low"], b["close"], b["volume"]):
            out.append(out[-1] if out else None)
            continue
        pv += (b["high"] + b["low"] + b["close"]) / 3 * b["volume"]
        vv += b["volume"]
        out.append(pv / vv if vv else None)
    return out


def cum_volume(bars: list[dict]) -> list[float]:
    out, s = [], 0.0
    for b in bars:
        s += b["volume"] or 0
        out.append(s)
    return out


def rvol_at(today: list[dict], priors: list[list[dict]], k: int) -> tuple[float | None, float | None, int]:
    """Cumulative volume through bar k today / average cumulative volume through bar k on prior days."""
    if k < 0 or k >= len(today):
        return None, None, 0
    t = cum_volume(today)[k]
    base = [cum_volume(d)[k] for d in priors if len(d) > k]
    if not base or not sum(base):
        return None, None, len(base)
    avg = sum(base) / len(base)
    return t / avg, avg, len(base)


def opening_range(bars: list[dict], minutes: int) -> tuple[float | None, float | None, int]:
    n = minutes // BAR_MIN
    if len(bars) < n:
        return None, None, n
    w = bars[:n]
    return max(b["high"] for b in w), min(b["low"] for b in w), n


# ---------- evaluation ----------

def _rule(rid, name, threshold, observed, result, margin=None, facts=()):
    return {"id": rid, "name": name, "threshold": threshold, "observed": observed, "result": result, "margin": margin, "facts": list(facts)}


def evaluate(sym: str, today: list[dict], priors: list[list[dict]], p: DayParams, quote: dict | None,
             mode: str, cursor: str, day: str) -> dict:
    rules, facts = [], {}
    k = len(today) - 1
    b = today[-1] if today else None
    vw = vwap(today)
    rv, rv_base, rv_days = rvol_at(today, priors, k)
    hi, lo, or_n = opening_range(today, p.or_minutes)
    bar_end = end_of(b["ts"]) if b else None
    facts["bar"] = {"label": "Latest completed 5-minute bar", "value": b and {k2: b[k2] for k2 in ("ts", "open", "high", "low", "close", "volume")},
                    "source": {"symbol": sym, "feed": b and b["source"], "bar_end": bar_end}}
    facts["vwap"] = {"label": "Session VWAP", "value": vw[-1] and round(vw[-1], 4) if vw else None,
                     "inputs": {"formula": "sum(typical price x volume) / sum(volume) since 09:30; typical = (H+L+C)/3", "bars": len(today)}}
    facts["rvol"] = {"label": "Relative volume (same time of day)", "value": rv and round(rv, 3),
                     "inputs": {"today_cumulative": cum_volume(today)[k] if today else None, "prior_avg_cumulative": rv_base and round(rv_base),
                                "prior_days_used": rv_days, "through_bar": b and b["ts"][11:]}}
    facts["opening_range"] = {"label": f"Opening range ({p.or_minutes} min)", "value": None if hi is None else {"high": hi, "low": lo, "mid": round((hi + lo) / 2, 4)},
                              "inputs": {"bars": or_n}}
    facts["quote"] = {"label": "Latest quote", "value": quote, "inputs": {"mode": mode}}

    enough = b is not None and rv_days >= 3
    rules.append(_rule("data", "Enough intraday data", "today's bars + >= 3 prior days for relative volume",
                       f"{len(today)} bars today, {rv_days} prior days", "pass" if enough else "unknown",
                       None if enough else "waiting for bars or intraday history", ["bar", "rvol"]))
    if b is None:
        status = {"status": "NEEDS_REVIEW", "failed": [], "unknown": ["data"]}
        return {"symbol": sym, "date": day, "cursor": cursor, "rules": rules, "facts": facts, **status,
                "suggested_stop": None, "target": None, "entry_assumption": None, "strategy": p.strategy,
                "strategy_name": DAY_STRATEGIES[p.strategy]["name"], "style": "intraday",
                "plan": {"entry": "next bar open", "flat_by": p.flat_by, "entry_date": day, "exit_date": day, "sessions": [day]}}

    t_end = bar_end[11:]
    in_win = p.window_start <= t_end <= p.window_end
    rules.append(_rule("in_window", "Inside the entry window", f"{p.window_start}-{p.window_end} ET", f"bar ends {t_end}",
                       "pass" if in_win else "fail", None, ["bar"]))
    c, prev_c = b["close"], (today[-2]["close"] if len(today) > 1 else None)
    stop = None
    if p.strategy == "vwap_reclaim":
        v, pv_ = vw[-1], (vw[-2] if len(vw) > 1 else None)
        ok = None not in (v, pv_, prev_c) and prev_c < pv_ and c > v
        rules.append(_rule("vwap_reclaim", "Crossed back above VWAP on this bar", "previous close < VWAP, close > VWAP",
                           "n/a" if None in (v, pv_, prev_c) else f"prev {prev_c:.2f} vs {pv_:.2f}; now {c:.2f} vs {v:.2f}",
                           "unknown" if None in (v, pv_, prev_c) else ("pass" if ok else "fail"),
                           None if v is None else f"{(c - v) / v * 100:+.2f}% vs VWAP", ["bar", "vwap"]))
        if v is not None:
            stop = round(min(b["low"], v) * (1 - p.stop_buffer_pct / 100), 2)
    else:
        if hi is None or len(today) <= or_n:
            rules.append(_rule("or_breakout", "First close above the opening-range high", "opening range complete",
                               f"{len(today)} of {or_n} range bars" if hi is None else "range just completed", "fail", None, ["opening_range"]))
        else:
            broke_before = any(x["close"] > hi for x in today[or_n:-1])
            ok = c > hi and not broke_before
            rules.append(_rule("or_breakout", "First close above the opening-range high", f"close > {hi:.2f}, first time today",
                               f"close {c:.2f}" + ("; already broke out earlier" if broke_before else ""),
                               "pass" if ok else "fail", f"{(c - hi) / hi * 100:+.2f}% vs range high", ["bar", "opening_range"]))
            base = (hi + lo) / 2 if p.orb_stop == "mid" else lo
            stop = round(base * (1 - p.stop_buffer_pct / 100), 2)
    rules.append(_rule("rvol", "Heavy relative volume", f">= {p.rvol_min:g}x usual for this time of day",
                       "n/a" if rv is None else f"{rv:.2f}x", "unknown" if rv is None else ("pass" if rv >= p.rvol_min else "fail"),
                       None if rv is None else f"{rv - p.rvol_min:+.2f}x vs threshold", ["rvol"]))
    if mode == "replay":
        rules.append(_rule("spread", "Tight bid-ask spread", f"<= {p.spread_max_bps:g} bps", "not available in replay", "info",
                           "historical quotes aren't loaded; not counted", ["quote"]))
        rules.append(_rule("quote_fresh", "Fresh quote", f"<= {p.quote_max_age_s:g} s old", "not available in replay", "info", None, ["quote"]))
    else:
        if not quote:
            rules.append(_rule("spread", "Tight bid-ask spread", f"<= {p.spread_max_bps:g} bps", "no quote", "unknown", None, ["quote"]))
            rules.append(_rule("quote_fresh", "Fresh quote", f"<= {p.quote_max_age_s:g} s old", "no quote", "unknown", None, ["quote"]))
        else:
            rules.append(_rule("spread", "Tight bid-ask spread", f"<= {p.spread_max_bps:g} bps", f"{quote['spread_bps']:.1f} bps",
                               "pass" if quote["spread_bps"] <= p.spread_max_bps else "fail", f"bid {quote['bid']} / ask {quote['ask']}", ["quote"]))
            rules.append(_rule("quote_fresh", "Fresh quote", f"<= {p.quote_max_age_s:g} s old", f"{quote['age_s']:.0f} s old",
                               "pass" if quote["age_s"] <= p.quote_max_age_s else "fail", None, ["quote"]))

    core = set(DAY_STRATEGIES[p.strategy]["core"]) | ({"spread", "quote_fresh"} if mode == "live" else set())
    fails = [r["id"] for r in rules if r["id"] in core and r["result"] == "fail"]
    unknowns = [r["id"] for r in rules if r["result"] == "unknown"]
    if rules[0]["result"] == "unknown":
        status = "NEEDS_REVIEW"
    elif not fails and not unknowns:
        status = "QUALIFIES"
    elif not fails:
        status = "NEEDS_REVIEW"
    elif len(fails) == 1:
        status = "NEAR_MISS"
    else:
        status = "NO_SETUP"
    entry = quote["ask"] if quote and mode == "live" else c
    if stop is not None and stop >= entry:
        stop = None
    target = None if stop is None else round(entry + p.target_r * (entry - stop), 2)
    return {"symbol": sym, "date": day, "cursor": cursor, "status": status, "failed": fails, "unknown": unknowns,
            "rules": rules, "facts": facts, "suggested_stop": stop, "target": target, "entry_assumption": round(entry, 2),
            "strategy": p.strategy, "strategy_name": DAY_STRATEGIES[p.strategy]["name"], "style": "intraday",
            "data_timestamp": f"{day} {cursor[11:]} ET (bar ended) | {b['source']}",
            "plan": {"entry": "next bar open", "flat_by": p.flat_by, "entry_date": day, "exit_date": day, "sessions": [day],
                     "target": target}}


# ---------- rules (versioned) ----------

def active_params(conn) -> tuple[DayParams, int]:
    r = conn.execute("SELECT * FROM day_rulesets ORDER BY version DESC LIMIT 1").fetchone()
    return (DayParams.from_dict(json.loads(r["params_json"])), r["version"]) if r else (DayParams(), 0)


def save_params(conn, params: dict) -> dict:
    p = DayParams.from_dict(params)
    v = (conn.execute("SELECT MAX(version) FROM day_rulesets").fetchone()[0] or 0) + 1
    conn.execute("INSERT INTO day_rulesets VALUES (?,?,?)", (v, json.dumps(asdict(p)), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return {"version": v, "params": asdict(p)}


# ---------- session clock ----------

def session(conn) -> dict:
    return db.kv_get(conn, "day_session") or {"mode": "live"}


def live_cursor(now: datetime | None = None) -> tuple[str, str, bool]:
    """(day, cursor, market_open) for the live session. Cursor = end of the last completed bar."""
    now = now or now_et()
    d = now.date()
    if not is_session(d):
        return d.isoformat(), f"{d.isoformat()}T09:30", False
    if now.time() < OPEN_T:
        return d.isoformat(), f"{d.isoformat()}T09:30", False
    if now.time() >= CLOSE_T:
        return d.isoformat(), f"{d.isoformat()}T16:00", False
    floored = now.replace(minute=now.minute - now.minute % BAR_MIN, second=0, microsecond=0)
    return d.isoformat(), _iso(floored), True


def current(conn) -> tuple[str, str, str]:
    s = session(conn)
    if s.get("mode") == "replay":
        return "replay", s["date"], s["cursor"]
    d, cur, _ = live_cursor()
    return "live", d, cur


def latest_quote(sym: str) -> dict | None:
    k, s, _ = credentials.get_alpaca(db.environment())
    if not k:
        return None
    try:
        with httpx.Client(base_url=DATA_URL, transport=TRANSPORT, timeout=10, headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}) as c:
            q = c.get("/v2/stocks/quotes/latest", params={"symbols": sym, "feed": "iex"}).json()["quotes"][sym]
        bid, ask = float(q["bp"]), float(q["ap"])
        if bid <= 0 or ask <= 0:
            return None
        ts = datetime.fromisoformat(q["t"][:26].rstrip("Z") + "+00:00") if "." in q["t"] else datetime.fromisoformat(q["t"].replace("Z", "+00:00"))
        mid = (bid + ask) / 2
        return {"bid": bid, "ask": ask, "spread_bps": round((ask - bid) / mid * 1e4, 2),
                "age_s": max(0.0, (datetime.now(timezone.utc) - ts).total_seconds()), "feed": "iex", "t": q["t"]}
    except Exception:
        return None


def latest_trades(symbols: list[str]) -> dict:
    """Latest trade per symbol from Alpaca (IEX feed, free with any Alpaca key). {} without keys or on errors."""
    k, s, _ = credentials.get_alpaca(db.environment())
    if not k or not symbols:
        return {}
    try:
        with httpx.Client(base_url=DATA_URL, transport=TRANSPORT, timeout=10, headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}) as c:
            trades = c.get("/v2/stocks/trades/latest", params={"symbols": ",".join(symbols), "feed": "iex"}).json().get("trades", {})
        return {sym: {"price": float(t["p"]), "t": t["t"], "feed": "iex"} for sym, t in trades.items() if t.get("p")}
    except Exception:
        return {}


# ---------- paper execution ----------

def _slip(px, side):
    return round(px * (1 + SLIPPAGE_BPS / 1e4) if side == "buy" else px * (1 - SLIPPAGE_BPS / 1e4), 4)


def process(conn, day: str, upto: str) -> list[str]:
    """Run the intraday simulator over every bar of `day` that completed after the last processed point."""
    key = f"day_processed:{day}"
    last = db.kv_get(conn, key) or f"{day}T09:30"
    events = []
    orders = conn.execute("SELECT * FROM orders WHERE broker='simulator' AND style='intraday' AND purpose='entry'").fetchall()
    if not orders:
        db.kv_set(conn, key, max(last, upto))
        return events
    syms = {o["symbol"] for o in orders}
    bars = {s: day_bars(conn, s, day) for s in syms}
    for o0 in orders:
        o = dict(conn.execute("SELECT * FROM orders WHERE id=?", (o0["id"],)).fetchone())
        for b in bars[o["symbol"]]:
            b_end = end_of(b["ts"])
            if b_end <= last or b_end > upto:
                continue
            o = dict(conn.execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone())
            plan = json.loads(conn.execute("SELECT plan_json FROM journal WHERE order_id=?", (o["id"],)).fetchone()["plan_json"])
            # entry: next bar open after the order was placed
            if o["status"] in ("pending", "partially_filled") and b["ts"] >= (o["eligible_from"] or ""):
                px = _slip(b["open"], "buy")
                sim._fill(conn, o, day, o["qty"] - o["filled_qty"], px, f"filled {o['qty']} @ {px:.2f} at {b['ts'][11:]} bar open")
                events.append(f"{o['symbol']}: day-trade entry filled @ {px:.2f}")
            held = sim._open_qty(conn, o["id"])
            if held <= 0:
                continue
            stop_row = conn.execute("SELECT * FROM orders WHERE parent_id=? AND purpose='stop' AND status='pending'", (o["id"],)).fetchone()
            tgt_row = conn.execute("SELECT * FROM orders WHERE parent_id=? AND purpose='target' AND status='pending'", (o["id"],)).fetchone()
            done = None
            if stop_row and b["low"] <= stop_row["stop_price"]:
                px = _slip(min(b["open"], stop_row["stop_price"]), "sell")
                conn.execute("UPDATE orders SET qty=? WHERE id=?", (held, stop_row["id"]))
                sim._fill(conn, dict(conn.execute("SELECT * FROM orders WHERE id=?", (stop_row["id"],)).fetchone()), day, held, px,
                          f"stop hit at {b['ts'][11:]}: sold {held} @ {px:.2f}")
                done = ("stop", px)
            elif tgt_row and b["high"] >= tgt_row["limit_price"]:
                px = round(max(b["open"], tgt_row["limit_price"]), 4)
                conn.execute("UPDATE orders SET qty=? WHERE id=?", (held, tgt_row["id"]))
                sim._fill(conn, dict(conn.execute("SELECT * FROM orders WHERE id=?", (tgt_row["id"],)).fetchone()), day, held, px,
                          f"target hit at {b['ts'][11:]}: sold {held} @ {px:.2f}")
                done = ("target", px)
            elif b_end[11:] >= plan.get("flat_by", "15:55"):
                fid = sim.insert_order(conn, client_order_id=f"{o['client_order_id']}:flat", symbol=o["symbol"], side="sell", qty=held,
                                       order_type="market_on_bar_close", limit_price=None, stop_price=None, purpose="flat",
                                       parent_id=o["id"], reason="flat by end of day")
                conn.execute("UPDATE orders SET style='intraday' WHERE id=?", (fid,))
                px = _slip(b["close"], "sell")
                sim._fill(conn, dict(conn.execute("SELECT * FROM orders WHERE id=?", (fid,)).fetchone()), day, held, px,
                          f"flat at {b_end[11:]}: sold {held} @ {px:.2f}")
                done = ("flat", px)
            if done:
                sim._close_children(conn, o["id"], f"{done[0]} filled")
                events.append(f"{o['symbol']}: day trade closed by {done[0]} @ {done[1]:.2f}")
    db.kv_set(conn, key, max(last, upto))
    return events


def manual_flatten(conn, entry_id: str, day: str, cursor: str) -> dict:
    held = sim._open_qty(conn, entry_id)
    e = conn.execute("SELECT * FROM orders WHERE id=?", (entry_id,)).fetchone()
    if not e or e["style"] != "intraday":
        raise ValueError("not a day-trade order")
    if held <= 0:
        raise ValueError("no open position")
    nxt = [b for b in day_bars(conn, e["symbol"], day) if b["ts"] >= cursor]
    when = nxt[0]["ts"] if nxt else None
    oid = sim.insert_order(conn, client_order_id=f"{e['client_order_id']}:manual:{uuid.uuid4().hex[:4]}", symbol=e["symbol"], side="sell",
                           qty=held, order_type="market", limit_price=None, stop_price=None, purpose="manual_exit",
                           parent_id=entry_id, reason="manual flatten at the next bar open", eligible_from=when)
    conn.execute("UPDATE orders SET style='intraday' WHERE id=?", (oid,))
    return {"order_id": oid, "fills_at": when}


def process_manual(conn, day: str, upto: str, last: str) -> list[str]:
    events = []
    for m in conn.execute("SELECT * FROM orders WHERE broker='simulator' AND style='intraday' AND purpose='manual_exit' AND status='pending'").fetchall():
        for b in day_bars(conn, m["symbol"], day):
            if b["ts"] >= (m["eligible_from"] or "") and end_of(b["ts"]) <= upto:
                held = sim._open_qty(conn, m["parent_id"])
                if held > 0:
                    px = _slip(b["open"], "sell")
                    conn.execute("UPDATE orders SET qty=? WHERE id=?", (held, m["id"]))
                    sim._fill(conn, dict(conn.execute("SELECT * FROM orders WHERE id=?", (m["id"],)).fetchone()), day, held, px,
                              f"manual flatten {held} @ {px:.2f} at {b['ts'][11:]} open")
                    sim._close_children(conn, m["parent_id"], "flattened manually")
                    events.append(f"{m['symbol']}: flattened manually @ {px:.2f}")
                else:
                    sim._set_status(conn, m["id"], "canceled", "no open position")
                break
    return events
