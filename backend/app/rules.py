"""Strategy rules: plain-language parsing into explicit parameters, and evaluation.

The evaluator is the single source of truth for pass / fail / unknown. The
explainer, the screener, the order preview, the journal, the forward test and the
historical replay all call `evaluate_at` (or `core_signal`, which runs the same
strategy rules), so they cannot disagree.

Strategy library (long-only, completed daily bars, entry at the next open, exit at
the stop or the close of the last planned session):

  ema_pullback      a quiet pullback that touches the EMA and closes back above it
  breakout          close above the prior N-session high on heavier volume, closing strong
  mean_reversion    deeply oversold (short RSI) while still above the long-term average
  trend_following   fast EMA crosses above a rising slow SMA; stop a multiple of ATR below
  earnings_drift    gap up after an earnings report that holds into the close on heavy volume

Shared for every strategy: the data rule, the earnings-in-window rule, status logic
(QUALIFIES / NEAR_MISS / NEEDS_REVIEW / NO_SETUP), the holding plan and sizing.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date

from .indicators import atr, ema, prior_max, rsi, sma, trailing_mean
from .tradingcal import next_sessions

DEFAULT_TEXT = ("Find stocks on my watchlist that pulled back to the 9-day EMA on lighter volume. "
                "Flag any company reporting earnings before my planned exit.")

STRATEGIES = {
    "ema_pullback": {
        "name": "EMA pullback", "style": "Buy a quiet dip in an uptrend",
        "template": DEFAULT_TEXT,
        "defaults": {"hold_sessions": 5, "stop_buffer_pct": 1.0},
        "params": ["ema_period", "volume_lookback", "volume_ratio_max"],
        "core": ("pullback_touch", "close_above", "lighter_volume"),
    },
    "breakout": {
        "name": "Breakout", "style": "Buy strength through a prior high",
        "template": ("Find stocks breaking out above their 20-day high on at least 1.5x average volume, closing near the high "
                     "of the day. Hold for 10 sessions and flag earnings before my planned exit."),
        "defaults": {"hold_sessions": 10, "stop_buffer_pct": 1.0},
        "params": ["breakout_lookback", "volume_lookback", "volume_ratio_min", "close_range_min"],
        "core": ("new_high", "heavier_volume", "strong_close"),
    },
    "mean_reversion": {
        "name": "Mean reversion", "style": "Buy a short-term washout in a long-term uptrend",
        "template": ("Find stocks above their 200-day moving average that are oversold, with the 2-day RSI below 10. "
                     "Hold for 5 sessions and flag earnings before my planned exit."),
        "defaults": {"hold_sessions": 5, "stop_buffer_pct": 3.0},
        "params": ["rsi_period", "rsi_max", "trend_sma"],
        "core": ("uptrend", "oversold"),
    },
    "trend_following": {
        "name": "Trend following", "style": "Join a new uptrend when the averages turn",
        "template": ("Find stocks where the 20-day EMA crosses above a rising 50-day moving average. Use a stop 2 ATR below "
                     "the close and hold for 20 sessions. Flag earnings before my planned exit."),
        "defaults": {"hold_sessions": 20, "stop_buffer_pct": 0.0},
        "params": ["fast_ema", "slow_sma", "slope_lookback", "atr_period", "atr_mult"],
        "core": ("cross_up", "slow_rising"),
    },
    "earnings_drift": {
        "name": "Post-earnings drift", "style": "Buy a gap up after earnings that holds",
        "template": ("Find stocks that gap up at least 3% right after an earnings report, hold the gap into the close "
                     "on at least 2x average volume. Hold for 10 sessions."),
        "defaults": {"hold_sessions": 10, "stop_buffer_pct": 1.0},
        "params": ["gap_min_pct", "volume_lookback", "volume_ratio_min"],
        "core": ("recent_report", "gap_up", "held_gap", "heavier_volume"),
    },
}


@dataclass
class RuleParams:
    strategy: str = "ema_pullback"
    # ema pullback
    ema_period: int = 9
    volume_lookback: int = 20
    volume_ratio_max: float = 0.80        # pullback: session volume must be BELOW this fraction of the average
    # breakout / drift
    breakout_lookback: int = 20
    volume_ratio_min: float = 1.5         # breakout/drift: session volume must be AT LEAST this multiple
    close_range_min: float = 0.5          # breakout: close in the top half of the day's range
    gap_min_pct: float = 3.0              # drift: open vs prior close
    # mean reversion
    rsi_period: int = 2
    rsi_max: float = 10.0
    trend_sma: int = 200
    # trend following
    fast_ema: int = 20
    slow_sma: int = 50
    slope_lookback: int = 5
    atr_period: int = 14
    atr_mult: float = 2.0
    # shared
    hold_sessions: int = 5
    earnings_mode: str = "warn"           # 'warn' -> flag only; 'exclude' -> drop the setup
    stop_buffer_pct: float = 1.0
    risk_per_trade_pct: float = 0.5       # of account equity
    max_position_pct: float = 20.0        # of account equity

    @classmethod
    def from_dict(cls, d: dict) -> "RuleParams":
        p = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        p.validate()
        return p

    def validate(self):
        checks = [
            (self.strategy in STRATEGIES, f"strategy must be one of {', '.join(STRATEGIES)}"),
            (2 <= self.ema_period <= 200, "EMA period must be 2-200"),
            (5 <= self.volume_lookback <= 100, "volume lookback must be 5-100 sessions"),
            (0.1 <= self.volume_ratio_max <= 1.5, "volume ratio threshold must be 0.10-1.50"),
            (5 <= self.breakout_lookback <= 260, "breakout lookback must be 5-260 sessions"),
            (1.0 <= self.volume_ratio_min <= 10, "minimum volume multiple must be 1-10"),
            (0 <= self.close_range_min <= 1, "close position in range must be 0-1"),
            (0.5 <= self.gap_min_pct <= 50, "gap must be 0.5-50%"),
            (2 <= self.rsi_period <= 30, "RSI period must be 2-30"),
            (1 <= self.rsi_max <= 50, "RSI threshold must be 1-50"),
            (20 <= self.trend_sma <= 300, "trend average must be 20-300 sessions"),
            (2 <= self.fast_ema < self.slow_sma <= 300, "fast EMA must be shorter than the slow SMA (max 300)"),
            (1 <= self.slope_lookback <= 60, "slope lookback must be 1-60 sessions"),
            (2 <= self.atr_period <= 60, "ATR period must be 2-60"),
            (0.5 <= self.atr_mult <= 10, "ATR multiple must be 0.5-10"),
            (1 <= self.hold_sessions <= 60, "holding period must be 1-60 sessions"),
            (self.earnings_mode in ("warn", "exclude"), "earnings_mode must be 'warn' or 'exclude'"),
            (0 <= self.stop_buffer_pct <= 20, "stop buffer must be 0-20%"),
            (0.05 <= self.risk_per_trade_pct <= 5, "risk per trade must be 0.05-5%"),
            (1 <= self.max_position_pct <= 100, "max position must be 1-100%"),
        ]
        for ok, msg in checks:
            if not ok:
                raise ValueError(msg)


# ---------- plain-language parsing ----------

def detect_strategy(t: str) -> str:
    if re.search(r"post[- ]earnings|after (?:an? )?earnings|earnings gap|gap(?:s|ped)? up|earnings drift|\bpead\b", t):
        return "earnings_drift"
    if re.search(r"break(?:ing|s)?[- ]?out|new high|\d+\s*-?\s*(?:day|session|week)s?\s+high|donchian", t):
        return "breakout"
    if re.search(r"oversold|\brsi\b|mean[- ]revers|snap ?back|washout", t):
        return "mean_reversion"
    if re.search(r"cross(?:es|ed|ing|over)?\s*(?:above|over)?|golden cross|trend[- ]follow", t) and not re.search(r"pull(?:ed|s)?\s*back", t):
        return "trend_following"
    return "ema_pullback"


def _num(m, cast=float):
    return cast(m.group(1)) if m else None


def parse_strategy(text: str) -> dict:
    """Deterministic translation of a plain-language setup into editable rules.

    The strategy is detected from the wording; every vague phrase gets an explicit,
    visible definition. Numbers the user states override the strategy's defaults.
    """
    t = text.lower()
    strat = detect_strategy(t)
    p = RuleParams(strategy=strat, **STRATEGIES[strat]["defaults"])
    interp = [{"phrase": f"strategy: {STRATEGIES[strat]['name']}",
               "definition": f"{STRATEGIES[strat]['style']}. Detected from your wording; pick another strategy to change it.",
               "params": ["strategy"]}]

    if strat == "ema_pullback":
        m = re.search(r"(\d+)\s*-?\s*(?:day|period|session)?\s*(?:ema|exponential)", t)
        if m:
            p.ema_period = int(m.group(1))
        interp.append({"phrase": f"pulled back to the {p.ema_period}-day EMA",
                       "definition": (f"On the latest completed daily bar, the session low touched or crossed below the "
                                      f"{p.ema_period}-day EMA and the close finished at or above it."),
                       "params": ["ema_period"]})
        if re.search(r"\bnear\b", t):
            interp.append({"phrase": "near", "definition": "'Near' is not used as a tolerance band: the low must actually reach the EMA.",
                           "params": []})
        m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:of\s*)?(?:the\s*)?(?:(?:prior|previous|last)\s*)?(?:\d+\s*-?\s*(?:day|session)s?\s*)?(?:average|avg)", t)
        if m:
            p.volume_ratio_max = float(m.group(1)) / 100
        m = re.search(r"(\d+)\s*-?\s*(?:day|session)s?\s*(?:average|avg)\s*volume", t)
        if m:
            p.volume_lookback = int(m.group(1))
        interp.append({"phrase": "lighter volume",
                       "definition": (f"Session volume below {p.volume_ratio_max:.0%} of the average volume of the previous "
                                      f"{p.volume_lookback} completed sessions (the signal session itself is excluded)."),
                       "params": ["volume_ratio_max", "volume_lookback"]})

    elif strat == "breakout":
        m = re.search(r"(\d+)\s*-?\s*(day|session|week)s?\s+high", t)
        if m:
            n = int(m.group(1))
            p.breakout_lookback = n * 5 if m.group(2) == "week" else n
        m = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(?:the\s*)?(?:average|avg|normal)?", t)
        if m:
            p.volume_ratio_min = float(m.group(1))
        interp.append({"phrase": f"breaking out above the {p.breakout_lookback}-session high",
                       "definition": (f"The close is above the highest high of the previous {p.breakout_lookback} completed "
                                      "sessions (the signal session itself is not part of the range)."),
                       "params": ["breakout_lookback"]})
        interp.append({"phrase": "on heavier volume",
                       "definition": (f"Session volume at least {p.volume_ratio_min:g}x the average of the previous "
                                      f"{p.volume_lookback} sessions."), "params": ["volume_ratio_min", "volume_lookback"]})
        interp.append({"phrase": "closing near the high of the day",
                       "definition": (f"The close is in the top {1 - p.close_range_min:.0%} of the day's range: "
                                      f"(close - low) / (high - low) >= {p.close_range_min:g}."), "params": ["close_range_min"]})

    elif strat == "mean_reversion":
        m = re.search(r"(\d+)\s*-?\s*(?:day|period|session)?\s*rsi|rsi\s*\(?\s*(\d+)\s*\)?", t)
        if m:
            p.rsi_period = int(m.group(1) or m.group(2))
        m = re.search(r"rsi[^.]*?(?:below|under|less than|<)\s*(\d+(?:\.\d+)?)", t)
        if m:
            p.rsi_max = float(m.group(1))
        m = re.search(r"(\d+)\s*-?\s*day\s*(?:simple\s*)?(?:sma|moving average|ma)\b", t)
        if m:
            p.trend_sma = int(m.group(1))
        interp.append({"phrase": f"above the {p.trend_sma}-day moving average",
                       "definition": f"The close is above the {p.trend_sma}-session simple moving average: the long-term trend is still up.",
                       "params": ["trend_sma"]})
        interp.append({"phrase": "oversold",
                       "definition": (f"The {p.rsi_period}-period RSI (Wilder) is below {p.rsi_max:g}: several down closes in a row, "
                                      "a short-term washout."), "params": ["rsi_period", "rsi_max"]})

    elif strat == "trend_following":
        m = re.search(r"(\d+)\s*/\s*(\d+)", t) or re.search(r"(\d+)\s*-?\s*day\s*(?:ema|ma|moving average)[^.]*?cross[^.]*?(\d+)\s*-?\s*day", t)
        if m:
            p.fast_ema, p.slow_sma = int(m.group(1)), int(m.group(2))
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:x\s*)?atr", t)
        if m:
            p.atr_mult = float(m.group(1))
        interp.append({"phrase": f"the {p.fast_ema}-day EMA crosses above the {p.slow_sma}-day moving average",
                       "definition": (f"On the signal session the {p.fast_ema}-day EMA closed above the {p.slow_sma}-day SMA, "
                                      "after being at or below it the session before. Only the day of the cross counts."),
                       "params": ["fast_ema", "slow_sma"]})
        interp.append({"phrase": "rising",
                       "definition": f"The {p.slow_sma}-day SMA is higher than it was {p.slope_lookback} sessions ago.",
                       "params": ["slope_lookback"]})
        interp.append({"phrase": f"stop {p.atr_mult:g} ATR below the close",
                       "definition": (f"Stop = close - {p.atr_mult:g} x the {p.atr_period}-session average true range (Wilder), "
                                      "so the stop widens when the stock is more volatile."), "params": ["atr_mult", "atr_period"]})

    elif strat == "earnings_drift":
        m = re.search(r"gap(?:s|ped)?\s*(?:up\s*)?(?:of\s*|by\s*)?(?:at least\s*)?(\d+(?:\.\d+)?)\s*%", t) or \
            re.search(r"(\d+(?:\.\d+)?)\s*%\s*gap", t)
        if m:
            p.gap_min_pct = float(m.group(1))
        m = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(?:the\s*)?(?:average|avg|normal)?", t)
        if m:
            p.volume_ratio_min = float(m.group(1))
        interp.append({"phrase": "right after an earnings report",
                       "definition": ("A report dated the signal session (before the open) or the session before it (after the close). "
                                      "Earnings dates come from the earnings calendar; a stock with no report on record can't qualify."),
                       "params": []})
        interp.append({"phrase": f"gap up at least {p.gap_min_pct:g}%",
                       "definition": f"Signal session open at least {p.gap_min_pct:g}% above the previous close.", "params": ["gap_min_pct"]})
        interp.append({"phrase": "hold the gap into the close",
                       "definition": "Close at or above the open: buyers kept the gap instead of selling it.", "params": []})
        interp.append({"phrase": "heavy volume",
                       "definition": (f"Session volume at least {p.volume_ratio_min:g}x the average of the previous "
                                      f"{p.volume_lookback} sessions."), "params": ["volume_ratio_min", "volume_lookback"]})

    m = re.search(r"(?:hold|exit)[^.]*?(\d+)\s*(?:trading\s*)?(?:day|session)s?", t)
    if m:
        p.hold_sessions = int(m.group(1))
    interp.append({"phrase": "planned exit",
                   "definition": (f"Enter at the next session's open after the signal; exit at the close of the "
                                  f"{p.hold_sessions}th trading session held (entry day counts as session 1), unless the stop is hit first."),
                   "params": ["hold_sessions"]})
    if "earnings" in t and strat != "earnings_drift" or re.search(r"earnings before", t):
        if re.search(r"exclude|skip|avoid|remove|drop", t):
            p.earnings_mode = "exclude"
        interp.append({"phrase": "flag any company reporting earnings before my planned exit",
                       "definition": ("A scheduled earnings date from the signal date's next session through the planned exit date "
                                      "is flagged. A missing next earnings date is labelled 'unknown', never assumed clear. "
                                      "You choose whether a flag excludes the stock or only warns."),
                       "params": ["earnings_mode"]})
    interp.append({"phrase": "(implied) completed daily bars",
                   "definition": "Only fully completed daily sessions are used; signals are known after the close and can be acted on at the next open.",
                   "params": []})
    interp.append({"phrase": "(implied) stop and size",
                   "definition": f"{stop_rule_text(p)} Size so a stop-out loses about {p.risk_per_trade_pct:g}% of equity, "
                                 f"capped at {p.max_position_pct:g}% of equity.",
                   "params": ["stop_buffer_pct", "risk_per_trade_pct", "max_position_pct"]})
    p.validate()
    return {"params": asdict(p), "interpretations": interp}


def stop_rule_text(p: RuleParams) -> str:
    return {
        "ema_pullback": f"Protective stop {p.stop_buffer_pct:g}% below the lower of the signal session's low and the EMA.",
        "breakout": f"Protective stop {p.stop_buffer_pct:g}% below the lower of the signal session's low and the breakout level.",
        "mean_reversion": f"Protective stop {p.stop_buffer_pct:g}% below the signal session's low.",
        "trend_following": f"Protective stop {p.atr_mult:g} ATR({p.atr_period}) below the signal close.",
        "earnings_drift": f"Protective stop {p.stop_buffer_pct:g}% below the gap day's low.",
    }[p.strategy]


# ---------- series with cached indicators ----------

CORE_RULES = STRATEGIES["ema_pullback"]["core"]   # kept for callers that predate the library


@dataclass
class Series:
    symbol: str
    dates: list[str]
    open: list[float | None]
    high: list[float | None]
    low: list[float | None]
    close: list[float | None]
    volume: list[float | None]
    source: str = ""
    _cache: dict = field(default_factory=dict, repr=False)

    def _c(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def ema(self, period: int) -> list[float | None]:
        return self._c(("ema", period), lambda: ema(self.close, period))

    def sma(self, period: int) -> list[float | None]:
        return self._c(("sma", period), lambda: sma(self.close, period))

    def rsi(self, period: int) -> list[float | None]:
        return self._c(("rsi", period), lambda: rsi(self.close, period))

    def atr(self, period: int) -> list[float | None]:
        return self._c(("atr", period), lambda: atr(self.high, self.low, self.close, period))

    def truncated(self, i: int) -> "Series":
        return Series(self.symbol, self.dates[:i + 1], self.open[:i + 1], self.high[:i + 1],
                      self.low[:i + 1], self.close[:i + 1], self.volume[:i + 1], self.source)


def _fmt(x, nd=2):
    return None if x is None else round(x, nd)


def _pct(a, b):
    return (a - b) / b * 100


# ---------- strategy cores ----------
# Each returns (rules, facts, stop, history_ok). Rules use result 'pass' | 'fail' | 'unknown'.

def _rule(rid, name, threshold, observed, result, margin=None, facts=()):
    return {"id": rid, "name": name, "threshold": threshold, "observed": observed, "result": result,
            "margin": margin, "facts": list(facts)}


def _volume_facts(s, i, p):
    avgv = trailing_mean(s.volume, i, p.volume_lookback)
    vol = s.volume[i]
    ratio = None if (avgv in (None, 0) or vol is None) else vol / avgv
    facts = {
        "avg_volume": {"label": f"Average volume, previous {p.volume_lookback} sessions", "value": _fmt(avgv, 0),
                       "inputs": {"sessions": [{"date": s.dates[j], "volume": s.volume[j]}
                                               for j in range(max(0, i - p.volume_lookback), i)],
                                  "excludes_signal_session": True},
                       "source": {"symbol": s.symbol, "dataset": s.source}},
        "volume_ratio": {"label": "Session volume / prior average", "value": _fmt(ratio, 4),
                         "inputs": {"session_volume": vol, "avg_volume": _fmt(avgv, 0)}},
    }
    return avgv, ratio, facts


def _core_pullback(s, i, p, ctx):
    e = s.ema(p.ema_period)[i]
    avgv, ratio, facts = _volume_facts(s, i, p)
    facts["ema"] = {"label": f"{p.ema_period}-day EMA at close", "value": _fmt(e, 4),
                    "inputs": {"period": p.ema_period, "alpha": round(2 / (p.ema_period + 1), 6),
                               "close_t": s.close[i], "ema_prev": _fmt(s.ema(p.ema_period)[i - 1], 4) if i else None,
                               "formula": f"EMA_t = {2 / (p.ema_period + 1):.4f} x close_t + {1 - 2 / (p.ema_period + 1):.4f} x EMA_(t-1)",
                               "seed": f"simple average of the first {p.ema_period} closes"},
                    "source": {"symbol": s.symbol, "date": s.dates[i], "dataset": s.source}}
    lo, c = s.low[i], s.close[i]
    rules = []
    if e is None or None in (lo, c):
        rules += [_rule("pullback_touch", "Low touched or crossed the EMA", "low <= EMA", "n/a", "unknown", None, ["bar", "ema"]),
                  _rule("close_above", "Close at or above the EMA", "close >= EMA", "n/a", "unknown", None, ["bar", "ema"])]
    else:
        rules += [_rule("pullback_touch", "Low touched or crossed the EMA", f"low <= EMA ({e:.2f})", f"low {lo:.2f}",
                        "pass" if lo <= e else "fail", f"low was {_pct(lo, e):+.2f}% vs EMA", ["bar", "ema"]),
                  _rule("close_above", "Close at or above the EMA", f"close >= EMA ({e:.2f})", f"close {c:.2f}",
                        "pass" if c >= e else "fail", f"close was {_pct(c, e):+.2f}% vs EMA", ["bar", "ema"])]
    th = f"volume < {p.volume_ratio_max:.0%} of {p.volume_lookback}-session avg"
    rules.append(_rule("lighter_volume", "Lighter volume", th, "n/a", "unknown", "volume history incomplete", ["avg_volume", "volume_ratio"])
                 if ratio is None else
                 _rule("lighter_volume", "Lighter volume", th, f"{ratio:.0%} of avg", "pass" if ratio < p.volume_ratio_max else "fail",
                       f"{(ratio - p.volume_ratio_max) * 100:+.1f} pts vs threshold", ["avg_volume", "volume_ratio"]))
    stop = None if e is None or lo is None else round(min(lo, e) * (1 - p.stop_buffer_pct / 100), 2)
    return rules, facts, stop, e is not None and avgv is not None, f">= {p.ema_period} closes for EMA and {p.volume_lookback} prior volumes"


def _core_breakout(s, i, p, ctx):
    level = prior_max(s.high, i, p.breakout_lookback)
    avgv, ratio, facts = _volume_facts(s, i, p)
    facts["breakout_level"] = {"label": f"Highest high of the previous {p.breakout_lookback} sessions", "value": _fmt(level, 4),
                               "inputs": {"sessions": [{"date": s.dates[j], "high": s.high[j]}
                                                       for j in range(max(0, i - p.breakout_lookback), i)],
                                          "excludes_signal_session": True},
                               "source": {"symbol": s.symbol, "dataset": s.source}}
    h, lo, c = s.high[i], s.low[i], s.close[i]
    pos = None if None in (h, lo, c) or h == lo else (c - lo) / (h - lo)
    facts["range_position"] = {"label": "Close position in the day's range", "value": _fmt(pos, 3),
                               "inputs": {"formula": "(close - low) / (high - low)", "close": c, "low": lo, "high": h}}
    rules = [
        _rule("new_high", f"Close above the {p.breakout_lookback}-session high", "close > prior high" if level is None else f"close > {level:.2f}",
              "n/a" if level is None or c is None else f"close {c:.2f}",
              "unknown" if level is None or c is None else ("pass" if c > level else "fail"),
              None if level is None or c is None else f"{_pct(c, level):+.2f}% vs prior high", ["bar", "breakout_level"]),
        _rule("heavier_volume", "Heavier volume", f"volume >= {p.volume_ratio_min:g}x {p.volume_lookback}-session avg",
              "n/a" if ratio is None else f"{ratio:.2f}x avg", "unknown" if ratio is None else ("pass" if ratio >= p.volume_ratio_min else "fail"),
              None if ratio is None else f"{ratio - p.volume_ratio_min:+.2f}x vs threshold", ["avg_volume", "volume_ratio"]),
        _rule("strong_close", "Close near the day's high", f"close in top {1 - p.close_range_min:.0%} of range",
              "n/a" if pos is None else f"{pos:.0%} of range", "unknown" if pos is None else ("pass" if pos >= p.close_range_min else "fail"),
              None, ["bar", "range_position"]),
    ]
    stop = None if level is None or lo is None else round(min(lo, level) * (1 - p.stop_buffer_pct / 100), 2)
    return rules, facts, stop, level is not None and avgv is not None, f">= {p.breakout_lookback} prior highs and {p.volume_lookback} prior volumes"


def _core_mean_reversion(s, i, p, ctx):
    m = s.sma(p.trend_sma)[i]
    r = s.rsi(p.rsi_period)[i]
    c, lo = s.close[i], s.low[i]
    facts = {
        "trend_sma": {"label": f"{p.trend_sma}-day simple moving average", "value": _fmt(m, 4),
                      "inputs": {"period": p.trend_sma, "formula": f"mean of the last {p.trend_sma} closes (inclusive)"},
                      "source": {"symbol": s.symbol, "dataset": s.source}},
        "rsi": {"label": f"RSI({p.rsi_period}), Wilder", "value": _fmt(r, 2),
                "inputs": {"period": p.rsi_period, "recent_closes": [{"date": s.dates[j], "close": s.close[j]} for j in range(max(0, i - 5), i + 1)],
                           "formula": "100 - 100 / (1 + avg gain / avg loss), Wilder smoothing"}},
    }
    rules = [
        _rule("uptrend", f"Close above the {p.trend_sma}-day average", "close > SMA" if m is None else f"close > {m:.2f}",
              "n/a" if m is None or c is None else f"close {c:.2f}", "unknown" if m is None or c is None else ("pass" if c > m else "fail"),
              None if m is None or c is None else f"{_pct(c, m):+.2f}% vs SMA", ["bar", "trend_sma"]),
        _rule("oversold", f"RSI({p.rsi_period}) oversold", f"RSI < {p.rsi_max:g}", "n/a" if r is None else f"RSI {r:.1f}",
              "unknown" if r is None else ("pass" if r < p.rsi_max else "fail"), None if r is None else f"{r - p.rsi_max:+.1f} vs threshold", ["rsi"]),
    ]
    stop = None if lo is None else round(lo * (1 - p.stop_buffer_pct / 100), 2)
    return rules, facts, stop, m is not None and r is not None, f">= {p.trend_sma} closes for the trend average"


def _core_trend(s, i, p, ctx):
    fe, sl = s.ema(p.fast_ema), s.sma(p.slow_sma)
    a = s.atr(p.atr_period)[i]
    f0, s0 = fe[i], sl[i]
    f1, s1 = (fe[i - 1], sl[i - 1]) if i else (None, None)
    sback = sl[i - p.slope_lookback] if i - p.slope_lookback >= 0 else None
    c = s.close[i]
    facts = {
        "fast_ema": {"label": f"{p.fast_ema}-day EMA", "value": _fmt(f0, 4), "inputs": {"previous_session": _fmt(f1, 4)}},
        "slow_sma": {"label": f"{p.slow_sma}-day SMA", "value": _fmt(s0, 4),
                     "inputs": {"previous_session": _fmt(s1, 4), f"{p.slope_lookback}_sessions_ago": _fmt(sback, 4)}},
        "atr": {"label": f"ATR({p.atr_period}), Wilder", "value": _fmt(a, 4),
                "inputs": {"formula": "true range = max(H-L, |H-prev C|, |L-prev C|), Wilder-smoothed", "multiple": p.atr_mult}},
    }
    known = None not in (f0, s0, f1, s1)
    crossed = known and f1 <= s1 and f0 > s0
    rules = [
        _rule("cross_up", f"{p.fast_ema}-day EMA crossed above the {p.slow_sma}-day SMA today", "EMA > SMA today, EMA <= SMA yesterday",
              "n/a" if not known else f"EMA {f0:.2f} vs SMA {s0:.2f} (yesterday {f1:.2f} vs {s1:.2f})",
              "unknown" if not known else ("pass" if crossed else "fail"),
              None if not known else ("crossed today" if crossed else ("already above" if f1 > s1 else "still below")), ["fast_ema", "slow_sma"]),
        _rule("slow_rising", f"{p.slow_sma}-day SMA rising", f"SMA today > SMA {p.slope_lookback} sessions ago",
              "n/a" if None in (s0, sback) else f"{s0:.2f} vs {sback:.2f}", "unknown" if None in (s0, sback) else ("pass" if s0 > sback else "fail"),
              None if None in (s0, sback) else f"{_pct(s0, sback):+.2f}%", ["slow_sma"]),
    ]
    stop = None if None in (a, c) else round(c - p.atr_mult * a, 2)
    return rules, facts, stop, known and a is not None, f">= {p.slow_sma} closes and {p.atr_period} ranges"


def _core_drift(s, i, p, ctx):
    past = ctx.get("past_earnings") or []
    prev_day = s.dates[i - 1] if i else None
    report = next((d for d in (s.dates[i], prev_day) if d and d in past), None)
    o, c, lo = s.open[i], s.close[i], s.low[i]
    pc = s.close[i - 1] if i else None
    gap = None if None in (o, pc) or not pc else (o / pc - 1) * 100
    avgv, ratio, facts = _volume_facts(s, i, p)
    facts["gap"] = {"label": "Opening gap vs previous close", "value": _fmt(gap, 3),
                    "inputs": {"open": o, "previous_close": pc, "formula": "(open / previous close - 1) x 100"}}
    facts["report"] = {"label": "Earnings report behind the gap", "value": report,
                       "inputs": {"accepted_dates": [x for x in (prev_day, s.dates[i]) if x], "reports_on_record": past[-3:]}}
    rules = [
        _rule("recent_report", "Earnings reported just before the gap", f"report on {prev_day} (after close) or {s.dates[i]} (before open)",
              report or "no report on record", "pass" if report else "fail", None, ["report"]),
        _rule("gap_up", "Gapped up", f"open >= {p.gap_min_pct:g}% above previous close", "n/a" if gap is None else f"{gap:+.2f}%",
              "unknown" if gap is None else ("pass" if gap >= p.gap_min_pct else "fail"), None, ["bar", "gap"]),
        _rule("held_gap", "Held the gap into the close", "close >= open", "n/a" if None in (o, c) else f"open {o:.2f}, close {c:.2f}",
              "unknown" if None in (o, c) else ("pass" if c >= o else "fail"), None, ["bar"]),
        _rule("heavier_volume", "Heavy volume", f"volume >= {p.volume_ratio_min:g}x {p.volume_lookback}-session avg",
              "n/a" if ratio is None else f"{ratio:.2f}x avg", "unknown" if ratio is None else ("pass" if ratio >= p.volume_ratio_min else "fail"),
              None if ratio is None else f"{ratio - p.volume_ratio_min:+.2f}x vs threshold", ["avg_volume", "volume_ratio"]),
    ]
    stop = None if lo is None else round(lo * (1 - p.stop_buffer_pct / 100), 2)
    return rules, facts, stop, avgv is not None and pc is not None, f">= {p.volume_lookback} prior volumes"


CORES = {"ema_pullback": _core_pullback, "breakout": _core_breakout, "mean_reversion": _core_mean_reversion,
         "trend_following": _core_trend, "earnings_drift": _core_drift}


def core_signal(s: Series, i: int, p: RuleParams, past_earnings: list[str] | None = None) -> tuple[bool, float | None]:
    """(all core rules pass, stop) on bar i, using bars[0..i] only. Same code as evaluate_at."""
    rules, _, stop, _, _ = CORES[p.strategy](s, i, p, {"past_earnings": past_earnings or []})
    return all(r["result"] == "pass" for r in rules), stop


def evaluate_at(s: Series, i: int, p: RuleParams, next_earnings: str | None, past_earnings: list[str],
                earnings_known: bool = True, expected_date: str | None = None, data_issue: str | None = None) -> dict:
    """Evaluate every rule on completed bar i using only bars[0..i]."""
    d = s.dates[i]
    bar = {"date": d, "open": s.open[i], "high": s.high[i], "low": s.low[i], "close": s.close[i], "volume": s.volume[i]}
    window = [x.isoformat() for x in next_sessions(date.fromisoformat(d), p.hold_sessions)]
    entry_date, exit_date = window[0], window[-1]
    core_rules, core_facts, stop, history_ok, history_need = CORES[p.strategy](s, i, p, {"past_earnings": past_earnings})

    facts = {
        "bar": {"label": "Signal session (completed daily bar)", "value": bar,
                "source": {"symbol": s.symbol, "date": d, "dataset": s.source}},
        **core_facts,
        "plan": {"label": "Holding plan", "value": {"entry_date": entry_date, "exit_date": exit_date, "sessions": window},
                 "inputs": {"hold_sessions": p.hold_sessions, "entry": "next session open", "exit": f"close of session {p.hold_sessions}",
                            "stop_rule": stop_rule_text(p)}},
        "earnings": {"label": "Next scheduled earnings", "value": next_earnings if earnings_known else None,
                     "inputs": {"last_reported": past_earnings[-1] if past_earnings else None},
                     "source": {"symbol": s.symbol, "dataset": s.source}},
    }

    have_price = None not in (s.low[i], s.close[i])
    fresh = (expected_date is None or d == expected_date) and not data_issue
    rules = [_rule("data", "Enough completed, current history",
                   history_need + (f"; latest bar = {expected_date}" if expected_date else ""),
                   f"{i + 1} sessions loaded, last {d}", "pass" if (history_ok and have_price and fresh) else "unknown",
                   (f"provider gap: latest bar has no {'/'.join(k for k, v in (('low', s.low[i]), ('close', s.close[i])) if v is None)}"
                    if not have_price else data_issue if data_issue else None if (history_ok and fresh) else
                    ("missing or insufficient bars" if not history_ok else f"stale: latest bar {d}, expected {expected_date}")),
                   ["bar"])]
    if not have_price:   # a provider gap makes every price rule unknown, never a pass or a fail
        core_rules = [{**r, "result": "unknown"} if "bar" in r["facts"] else r for r in core_rules]
    rules += core_rules

    ne = next_earnings if earnings_known else None
    if d in past_earnings and p.strategy != "earnings_drift":
        res = "fail" if p.earnings_mode == "exclude" else "warn"
        rules.append(_rule("earnings", "No earnings before planned exit", f"no report {d} .. {exit_date}",
                           f"reports {d} (signal day)", res, "a report after the close would gap the entry open", ["earnings", "plan"]))
    elif ne is None:
        rules.append(_rule("earnings", "No earnings before planned exit", f"no report {entry_date} .. {exit_date}",
                           "next date unknown", "unknown", "earnings date missing from calendar", ["earnings", "plan"]))
    elif entry_date <= ne <= exit_date:
        res = "fail" if p.earnings_mode == "exclude" else "warn"
        rules.append(_rule("earnings", "No earnings before planned exit", f"no report {entry_date} .. {exit_date}",
                           f"reports {ne}", res, "inside holding window", ["earnings", "plan"]))
    else:
        rules.append(_rule("earnings", "No earnings before planned exit", f"no report {entry_date} .. {exit_date}",
                           f"reports {ne}", "pass", "outside holding window", ["earnings", "plan"]))

    core_ids = STRATEGIES[p.strategy]["core"]
    by = {r["id"]: r for r in rules}
    hard = [r for r in rules if r["id"] in core_ids or (r["id"] == "earnings" and p.earnings_mode == "exclude")]
    fails = [r["id"] for r in hard if r["result"] == "fail"]
    unknowns = [r["id"] for r in rules if r["result"] == "unknown"]
    if by["data"]["result"] == "unknown":
        status = "NEEDS_REVIEW"   # can't trust any verdict without the history behind it
    elif not fails and not unknowns:
        status = "QUALIFIES_WITH_WARNING" if by["earnings"]["result"] == "warn" else "QUALIFIES"
    elif not fails and unknowns:
        status = "NEEDS_REVIEW"
    elif len(fails) == 1:
        status = "NEAR_MISS"
    else:
        status = "NO_SETUP"
    if not have_price:
        stop = None
    return {"symbol": s.symbol, "date": d, "status": status, "failed": fails, "unknown": unknowns,
            "rules": rules, "facts": facts, "suggested_stop": stop, "strategy": p.strategy,
            "strategy_name": STRATEGIES[p.strategy]["name"],
            "entry_assumption": s.close[i], "plan": facts["plan"]["value"]}


def signal_flags(s: Series, p: RuleParams, past_earnings: list[str] | None = None) -> list[bool]:
    """Per-bar core-signal check for the replay (the same strategy code as evaluate_at)."""
    return [core_signal(s, i, p, past_earnings)[0] for i in range(len(s.dates))]


def warmup(p: RuleParams) -> int:
    """Sessions of history a strategy needs before its first possible signal."""
    return max(p.volume_lookback + 1, {"ema_pullback": p.ema_period, "breakout": p.breakout_lookback + 1,
                                       "mean_reversion": p.trend_sma, "trend_following": max(p.slow_sma + p.slope_lookback, p.atr_period + 1),
                                       "earnings_drift": 2}[p.strategy])


def overlays(s: Series, p: RuleParams) -> list[dict]:
    """Chart lines that explain the strategy (values aligned to s.dates)."""
    if p.strategy == "ema_pullback":
        return [{"name": f"EMA {p.ema_period}", "values": s.ema(p.ema_period)}]
    if p.strategy == "breakout":
        return [{"name": f"{p.breakout_lookback}-day high", "values": [prior_max(s.high, i, p.breakout_lookback) for i in range(len(s.dates))]}]
    if p.strategy == "mean_reversion":
        return [{"name": f"SMA {p.trend_sma}", "values": s.sma(p.trend_sma)}]
    if p.strategy == "trend_following":
        return [{"name": f"EMA {p.fast_ema}", "values": s.ema(p.fast_ema)}, {"name": f"SMA {p.slow_sma}", "values": s.sma(p.slow_sma)}]
    return []
