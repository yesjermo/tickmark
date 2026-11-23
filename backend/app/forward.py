"""Forward (live) testing of the confirmed rules.

Unlike a historical replay, a forward test can't peek: a signal is recorded only
when the screener actually sees it on the latest completed bar, with the evidence
frozen at that moment. Each recorded signal is then followed on real incoming
bars with the same entry/exit rules as the paper plan:

  entry  = the first session open that was still in the future when the signal
           was seen (+ slippage), once that bar exists
  exit   = stop (min(signal low, EMA) - buffer) if a later low reaches it,
           otherwise the close of session `hold_sessions` (entry day = 1)
  bench  = equal-weight buy-and-hold of the watchlist over the same dates

Nothing is back-filled: if the app wasn't run on a day, that day's signals were
never seen and are not counted.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone

from . import db
from .backtest import wilson
from .brokers.simulator import SLIPPAGE_BPS

MIN_CLOSED = 20


def _slip(px, side):
    return px * (1 + SLIPPAGE_BPS / 1e4) if side == "buy" else px * (1 - SLIPPAGE_BPS / 1e4)


def record(conn, results: list[dict], ruleset: dict, source: str, eligible_from: str | None = None) -> list[dict]:
    """Insert each qualifying setup once. `eligible_from` is the first session whose open was still in the
    future when the app saw the signal, so a signal noticed mid-session can't 'enter' at an open already printed."""
    if not ruleset or ruleset.get("version") is None:
        return []
    added = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for ev in results:
        if not ev["status"].startswith("QUALIFIES") or ev.get("suggested_stop") is None:
            continue
        cur = conn.execute(
            """INSERT OR IGNORE INTO forward_signals(symbol,signal_date,ruleset_version,status_at_signal,evidence_json,
               plan_json,stop,recorded_at,data_source,eligible_from) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (ev["symbol"], ev["date"], ruleset["version"], ev["status"], json.dumps(ev),
             json.dumps({**ev["plan"], "hold_sessions": ruleset["params"]["hold_sessions"]}), ev["suggested_stop"], now, source,
             eligible_from))
        if cur.rowcount:
            added.append(ev)
    return added


def _bench(series: dict, start: str, end: str) -> float | None:
    vals = []
    for s in series.values():
        if start in s.dates and end in s.dates:
            a, b = s.open[s.dates.index(start)], s.close[s.dates.index(end)]
            if a and b:
                vals.append(b / a - 1)
    return round(sum(vals) / len(vals) * 100, 3) if vals else None


def _ok(s, k) -> bool:
    return None not in (s.open[k], s.low[k], s.close[k])


def update(conn, series: dict) -> None:
    for r in conn.execute("SELECT * FROM forward_signals WHERE state != 'closed'").fetchall():
        s = series.get(r["symbol"])
        if not s or r["signal_date"] not in s.dates:
            continue
        i = s.dates.index(r["signal_date"])
        hold = json.loads(r["plan_json"])["hold_sessions"]
        ef = r["eligible_from"] if "eligible_from" in r.keys() else None
        # Entry: first usable bar after the signal whose open was still in the future when the signal was seen.
        j = next((k for k in range(i + 1, len(s.dates)) if (ef is None or s.dates[k] >= ef) and _ok(s, k)), None)
        if j is None:
            continue
        entry = _slip(s.open[j], "buy")
        stop = r["stop"]
        last = min(j + hold - 1, len(s.dates) - 1)
        exit_px = exit_i = reason = None
        mark = j
        for k in range(j, last + 1):
            if not _ok(s, k):
                continue  # provider gap: skip the bar rather than treat a missing price as zero
            mark = k
            if s.low[k] <= stop:
                exit_px, exit_i, reason = _slip(min(s.open[k], stop), "sell"), k, "stop"
                break
        if exit_px is None and last == j + hold - 1 and mark == last:
            exit_px, exit_i, reason = _slip(s.close[last], "sell"), last, "time"
        fields = {"entry_date": s.dates[j], "entry_price": round(entry, 4),
                  "last_date": s.dates[mark], "last_close": s.close[mark]}
        if exit_px is not None:
            fields.update(state="closed", exit_date=s.dates[exit_i], exit_price=round(exit_px, 4), exit_reason=reason,
                          return_pct=round((exit_px / entry - 1) * 100, 3),
                          bench_return_pct=_bench(series, s.dates[j], s.dates[exit_i]))
        else:
            fields.update(state="open", return_pct=round((s.close[mark] / entry - 1) * 100, 3),
                          bench_return_pct=_bench(series, s.dates[j], s.dates[mark]))
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE forward_signals SET {sets} WHERE id=?", (*fields.values(), r["id"]))


def summary(conn, version: int | None) -> dict:
    rows = [dict(r) for r in conn.execute("SELECT * FROM forward_signals ORDER BY signal_date DESC, symbol")]
    for r in rows:
        r["evidence"] = json.loads(r.pop("evidence_json"))
        r["plan"] = json.loads(r.pop("plan_json"))
    cur = [r for r in rows if r["ruleset_version"] == version]
    closed = [r for r in cur if r["state"] == "closed"]
    rets = [r["return_pct"] for r in closed]
    excess = [r["return_pct"] - r["bench_return_pct"] for r in closed if r["bench_return_pct"] is not None]
    wins = sum(x > 0 for x in rets)
    first = min((r["recorded_at"] for r in cur), default=None)
    stats = {
        "recorded": len(cur), "waiting_entry": sum(r["state"] == "waiting_entry" for r in cur),
        "open": sum(r["state"] == "open" for r in cur), "closed": len(closed),
        "median_return_pct": round(statistics.median(rets), 3) if rets else None,
        "win_rate_pct": round(wins / len(rets) * 100, 1) if rets else None, "win_rate_95ci": wilson(wins, len(rets)),
        "mean_excess_vs_bench_pct": round(statistics.mean(excess), 3) if excess else None,
        "stops_hit": sum(r["exit_reason"] == "stop" for r in closed), "running_since": first,
    }
    if not cur:
        warn = ("No signals recorded yet for this rule version. A forward test only counts setups the app sees on the latest "
                "completed bar, so it starts empty and grows each session you refresh.")
    elif len(closed) < MIN_CLOSED:
        warn = (f"{len(closed)} closed signal(s) so far. Forward results need at least {MIN_CLOSED} closed signals, and ideally "
                f"months of different market conditions, before they say anything about the rules.")
    else:
        warn = None
    return {"ruleset_version": version, "stats": stats, "sample_warning": warn, "signals": rows,
            "method": {
                "recording": "A signal is recorded only when the screener sees it on the latest completed bar; evidence is frozen then. Nothing is back-filled.",
                "entry": f"first session open that was still in the future when the signal was seen, + {SLIPPAGE_BPS:g} bps",
                "exit": "stop if a later session's low reaches it (fills at min(open, stop)), else the close of the last planned session",
                "benchmark": "equal-weight buy-and-hold of the watchlist from the same entry open to the same exit close",
                "costs": f"{SLIPPAGE_BPS:g} bps slippage each side; no commissions",
            }}
