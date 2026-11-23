"""Indicator math. Pure functions; every value at index i uses only inputs at indices <= i."""
from __future__ import annotations


def ema(values: list[float | None], period: int) -> list[float | None]:
    """Exponential moving average.

    Seeded with the simple average of the first `period` values, then
    EMA_t = alpha * x_t + (1 - alpha) * EMA_{t-1}, alpha = 2 / (period + 1).
    Any missing value (None) breaks the chain: EMA is None until a fresh
    `period`-length run of values re-seeds it.
    """
    alpha = 2.0 / (period + 1)
    out: list[float | None] = [None] * len(values)
    run: list[float] = []
    prev: float | None = None
    for i, x in enumerate(values):
        if x is None:
            prev, run = None, []
            continue
        if prev is None:
            run.append(x)
            if len(run) == period:
                prev = sum(run) / period
                out[i] = prev
            continue
        prev = alpha * x + (1 - alpha) * prev
        out[i] = prev
    return out


def ema_trace(values: list[float | None], period: int, i: int) -> dict:
    """Inputs behind EMA[i], for click-through explanations."""
    series = ema(values, period)
    alpha = 2.0 / (period + 1)
    return {
        "formula": f"EMA_t = {alpha:.4f} x close_t + {1 - alpha:.4f} x EMA_(t-1)",
        "alpha": round(alpha, 6),
        "period": period,
        "close_t": values[i],
        "ema_prev": None if i == 0 else series[i - 1],
        "ema_t": series[i],
        "seed": f"simple average of the first {period} closes in the series",
    }


def trailing_mean(values: list[float | None], i: int, n: int) -> float | None:
    """Mean of values[i-n : i] (the n sessions BEFORE i, excluding i).

    Returns None if there are fewer than n prior values or any is missing.
    """
    if i - n < 0:
        return None
    window = values[i - n:i]
    if any(v is None for v in window):
        return None
    return sum(window) / n


def sma(values: list[float | None], period: int) -> list[float | None]:
    """Simple moving average of the last `period` values (inclusive). None if any is missing."""
    out: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        w = values[i - period + 1:i + 1]
        if None not in w:
            out[i] = sum(w) / period
    return out


def rsi(closes: list[float | None], period: int) -> list[float | None]:
    """Wilder's RSI. Seeded with simple averages of the first `period` gains/losses, then
    avg = (prev_avg x (period - 1) + current) / period. A missing close restarts the series."""
    out: list[float | None] = [None] * len(closes)
    gains: list[float] = []
    losses: list[float] = []
    ag = al = None
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a is None or b is None:
            gains, losses, ag, al = [], [], None, None
            continue
        ch = b - a
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if ag is None:
            gains.append(g)
            losses.append(l)
            if len(gains) == period:
                ag, al = sum(gains) / period, sum(losses) / period
            else:
                continue
        else:
            ag = (ag * (period - 1) + g) / period
            al = (al * (period - 1) + l) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(high: list[float | None], low: list[float | None], close: list[float | None], period: int) -> list[float | None]:
    """Wilder's average true range. True range = max(high - low, |high - prev close|, |low - prev close|)."""
    out: list[float | None] = [None] * len(close)
    trs: list[float] = []
    prev_atr = None
    for i in range(1, len(close)):
        h, l, pc = high[i], low[i], close[i - 1]
        if None in (h, l, pc):
            trs, prev_atr = [], None
            continue
        tr = max(h - l, abs(h - pc), abs(l - pc))
        if prev_atr is None:
            trs.append(tr)
            if len(trs) == period:
                prev_atr = sum(trs) / period
                out[i] = prev_atr
            continue
        prev_atr = (prev_atr * (period - 1) + tr) / period
        out[i] = prev_atr
    return out


def prior_max(values: list[float | None], i: int, n: int) -> float | None:
    """Highest of values[i-n : i] (the n sessions BEFORE i). None if incomplete."""
    if i - n < 0:
        return None
    w = values[i - n:i]
    return None if None in w else max(w)


def macd(closes: list[float | None], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list, list, list]:
    """MACD line (EMA fast - EMA slow), its signal line (EMA of the MACD line) and the histogram."""
    f, s = ema(closes, fast), ema(closes, slow)
    line = [None if a is None or b is None else a - b for a, b in zip(f, s)]
    sig = ema(line, signal)
    hist = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def bollinger(closes: list[float | None], period: int = 20, k: float = 2.0) -> tuple[list, list, list]:
    """Middle band = SMA(period); upper/lower = middle +/- k x population standard deviation of the same window."""
    mid = sma(closes, period)
    up: list[float | None] = [None] * len(closes)
    lo: list[float | None] = [None] * len(closes)
    for i, m in enumerate(mid):
        if m is None:
            continue
        w = closes[i - period + 1:i + 1]
        sd = (sum((x - m) ** 2 for x in w) / period) ** 0.5
        up[i], lo[i] = m + k * sd, m - k * sd
    return mid, up, lo
