"""NYSE trading calendar computed from the exchange's holiday rules, for any year.

Full-day closures: New Year's Day, Martin Luther King Jr. Day, Presidents' Day,
Good Friday, Memorial Day, Juneteenth (from 2022), Independence Day, Labor Day,
Thanksgiving and Christmas. A holiday on Saturday is observed the Friday before,
except New Year's Day (NYSE does not close the prior Friday); on Sunday it is
observed the Monday after. One-off closures (e.g. national days of mourning) are
listed in SPECIAL_CLOSURES. Early-close half days are treated as full sessions.
"""
from datetime import date, timedelta
from functools import lru_cache

SPECIAL_CLOSURES = {
    date(2025, 1, 9),   # national day of mourning, President Carter
}


def _nth_weekday(y, m, weekday, n):
    d = date(y, m, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y, m, weekday):
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(y):  # Anonymous Gregorian algorithm
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(y, month, day)


def _observed(d, allow_friday=True):
    if d.weekday() == 5:
        return d - timedelta(days=1) if allow_friday else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def holidays(y: int) -> frozenset:
    out = {
        _observed(date(y, 1, 1), allow_friday=False),
        _nth_weekday(y, 1, 0, 3),              # MLK Day
        _nth_weekday(y, 2, 0, 3),              # Presidents' Day
        _easter(y) - timedelta(days=2),        # Good Friday
        _last_weekday(y, 5, 0),                # Memorial Day
        _observed(date(y, 7, 4)),
        _nth_weekday(y, 9, 0, 1),              # Labor Day
        _nth_weekday(y, 11, 3, 4),             # Thanksgiving
        _observed(date(y, 12, 25)),
    }
    if y >= 2022:
        out.add(_observed(date(y, 6, 19)))     # Juneteenth
    out.discard(None)
    return frozenset(out | {d for d in SPECIAL_CLOSURES if d.year == y})


def is_session(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def sessions_between(start: date, end: date) -> list[date]:
    """Trading sessions in [start, end] inclusive."""
    out, d = [], start
    while d <= end:
        if is_session(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def next_sessions(after: date, n: int) -> list[date]:
    """The n trading sessions strictly after `after`."""
    out, d = [], after
    while len(out) < n:
        d += timedelta(days=1)
        if is_session(d):
            out.append(d)
    return out
