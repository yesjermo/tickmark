"""Compact historical replay of the confirmed rules on the watchlist.

Assumptions (also returned in the API response so the UI shows them):
  * A signal is known only after the signal session's close. Earliest entry is
    the NEXT session's open, plus slippage.
  * Exit: the strategy's protective stop (see rules.stop_rule_text) if a later
    session's low reaches it - fills at min(open, stop) minus slippage -
    otherwise the close of session `hold_sessions` (entry day = session 1).
  * Costs: SLIPPAGE_BPS on each side, no commission.
  * Overlap: one position per ticker at a time; signals while a position is open
    are skipped. Each trade gets a fixed 1/N of equity (N = watchlist size), so
    trades on different tickers can overlap without leverage.
  * Trades whose planned exit falls after the last available bar are left out.
  * Earnings filter (exclude mode) uses the scheduled-date calendar, assumed
    known at signal time.
"""
from __future__ import annotations

import math
import statistics
from datetime import date

from .brokers.simulator import SLIPPAGE_BPS
from .rules import STRATEGIES, RuleParams, Series, core_signal, stop_rule_text, warmup
from .tradingcal import next_sessions

MIN_TRADES = 30


def _slip(px, side):
    return px * (1 + SLIPPAGE_BPS / 1e4) if side == "buy" else px * (1 - SLIPPAGE_BPS / 1e4)


def trades_for(s: Series, p: RuleParams, earnings: list[str]) -> tuple[list[dict], int]:
    signals = [core_signal(s, i, p, earnings) for i in range(len(s.dates))]   # same strategy code as the screener
    n = len(s.dates)
    trades, skipped_incomplete, skipped_missing, free_from = [], 0, 0, 0
    for i in range(n):
        if not signals[i][0] or signals[i][1] is None or i < free_from:
            continue
        if p.earnings_mode == "exclude":
            window = next_sessions(date.fromisoformat(s.dates[i]), p.hold_sessions)
            if any(window[0].isoformat() <= d <= window[-1].isoformat() for d in earnings):
                continue
        last = i + p.hold_sessions
        if last > n - 1:
            skipped_incomplete += 1
            continue
        window = range(i + 1, last + 1)
        if s.open[i + 1] is None or any(None in (s.open[k], s.low[k], s.close[k]) for k in window):
            skipped_missing += 1
            continue
        entry = _slip(s.open[i + 1], "buy")
        stop = signals[i][1]
        if stop >= entry:     # e.g. a gap down through the planned stop: no trade
            skipped_missing += 1
            continue
        exit_px, exit_i, reason = None, last, "time"
        for k in range(i + 1, last + 1):
            if s.low[k] <= stop:
                exit_px, exit_i, reason = _slip(min(s.open[k], stop), "sell"), k, "stop"
                break
        if exit_px is None:
            exit_px = _slip(s.close[last], "sell")
        trades.append({"symbol": s.symbol, "signal_date": s.dates[i], "entry_date": s.dates[i + 1],
                       "entry": round(entry, 4), "stop": round(stop, 4), "exit_date": s.dates[exit_i],
                       "exit": round(exit_px, 4), "exit_reason": reason, "return_pct": round((exit_px / entry - 1) * 100, 3)})
        free_from = exit_i
    return trades, skipped_incomplete + skipped_missing


def _max_dd(curve: list[float]) -> float:
    peak, dd = -math.inf, 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return dd


def wilson(k: int, n: int, z=1.96):
    if n == 0:
        return None
    ph = k / n
    den = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / den
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return [round((c - h) * 100, 1), round((c + h) * 100, 1)]


def run(series: dict[str, Series], p: RuleParams, earnings: dict[str, list[str]]) -> dict:
    all_trades, incomplete = [], 0
    for sym, s in series.items():
        t, inc = trades_for(s, p, earnings.get(sym, []))
        all_trades += t
        incomplete += inc
    all_trades.sort(key=lambda t: (t["exit_date"], t["symbol"]))
    w = 1 / max(1, len(series))
    eq, curve, points = 1.0, [1.0], []
    for t in all_trades:
        eq *= 1 + w * t["return_pct"] / 100
        curve.append(eq)
        points.append({"date": t["exit_date"], "equity": round(eq, 5)})

    # Benchmark: equal-weight buy-and-hold over the same window the strategy could trade.
    # Common dates only, so tickers with shorter histories don't break the comparison.
    warm = warmup(p)
    common = sorted(set.intersection(*(set(s.dates) for s in series.values())))
    if len(common) < 2:
        raise ValueError("These tickers share too few trading dates for a buy-and-hold comparison.")
    idx = {sym: {d: k for k, d in enumerate(s.dates)} for sym, s in series.items()}
    start_i = min(warm + 1, len(common) - 1)
    start, end = common[start_i], common[-1]
    bench_curve, bench_points = [], []
    for d in common[start_i:]:
        v = 0.0
        for sym, s in series.items():
            c, o = s.close[idx[sym][d]], s.open[idx[sym][start]]
            if c is None or not o:   # provider gap: carry the previous value for this day
                v = None
                break
            v += w * c / o
        if v is None:
            continue
        bench_curve.append(v)
        bench_points.append({"date": d, "equity": round(v, 5)})

    rets = [t["return_pct"] for t in all_trades]
    wins = sum(r > 0 for r in rets)
    n = len(rets)
    return {
        "window": {"start": start, "end": end},
        "assumptions": {
            "entry": "next session open after the signal close (signal not tradable intraday)",
            "exit": f"{stop_rule_text(p)} Otherwise the close of session {p.hold_sessions} (entry day = 1).",
            "strategy": STRATEGIES[p.strategy]["name"],
            "costs": f"{SLIPPAGE_BPS:g} bps slippage each side; no commissions",
            "overlap": "one position per ticker; signals during an open trade are skipped; each trade sized at 1/N of equity",
            "data": "only bars available up to each decision date; EMA and volume average use past sessions only",
            "earnings": ("exclude mode skips signals with a scheduled report in the holding window" if p.earnings_mode == "exclude"
                         else "warn mode: earnings do not filter historical trades"),
            "excluded": f"{incomplete} signal(s) left out: planned exit after the last bar, or a missing price inside the trade",
        },
        "stats": {
            "trades": n, "median_return_pct": round(statistics.median(rets), 3) if rets else None,
            "mean_return_pct": round(statistics.mean(rets), 3) if rets else None,
            "win_rate_pct": round(wins / n * 100, 1) if n else None, "win_rate_95ci": wilson(wins, n),
            "max_drawdown_pct": round(_max_dd(curve) * 100, 2), "total_return_pct": round((eq - 1) * 100, 2),
            "stops_hit": sum(t["exit_reason"] == "stop" for t in all_trades),
        },
        "benchmark": {
            "definition": f"Equal-weight buy-and-hold of {', '.join(series)} from the {start} open to the {end} close, no rebalancing, no costs",
            "total_return_pct": round((bench_curve[-1] - 1) * 100, 2) if bench_curve else None,
            "max_drawdown_pct": round(_max_dd(bench_curve) * 100, 2) if bench_curve else None,
        },
        "sample_warning": (None if n >= MIN_TRADES else
                           f"Only {n} trades. That is too few to tell skill from luck: the 95% range for the win rate is "
                           f"{wilson(wins, n)[0] if n else 0}%-{wilson(wins, n)[1] if n else 100}%. Treat these results as a sanity check, not evidence."),
        "drawdown_note": "Strategy drawdown is marked at trade exits, so it understates drawdowns inside open trades. Benchmark drawdown is marked daily.",
        "equity_curve": points, "benchmark_curve": bench_points, "trades": all_trades,
    }
