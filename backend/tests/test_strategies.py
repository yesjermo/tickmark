"""Strategy library: indicators, each strategy's rules on constructed series, parsing, and consistency."""
import pytest

from app import backtest, demo_data
from app.indicators import atr, rsi, sma
from app.rules import STRATEGIES, RuleParams, Series, core_signal, evaluate_at, parse_strategy, signal_flags


def series(closes, highs=None, lows=None, opens=None, vols=None, start="2026-01-02"):
    from datetime import date
    from app.tradingcal import next_sessions
    n = len(closes)
    days = [date.fromisoformat(start)] + next_sessions(date.fromisoformat(start), n - 1)
    return Series("TST", [d.isoformat() for d in days], opens or list(closes), highs or [c * 1.01 for c in closes],
                  lows or [c * 0.99 for c in closes], list(closes), vols or [1_000_000] * n, "test")


def status(s, p, past=None, nxt="2027-12-31"):
    return evaluate_at(s, len(s.dates) - 1, p, nxt, past or [])


# ---------- indicators ----------

def test_sma():
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    assert sma([1, None, 3, 4], 2) == [None, None, None, 3.5]


def test_rsi_wilder_by_hand():
    out = rsi([1, 2, 3, 2], 2)
    assert out[:2] == [None, None]
    assert out[2] == 100.0                       # two gains, no losses
    assert out[3] == pytest.approx(50.0)         # ag=(1+0)/2=0.5, al=(0+1)/2=0.5


def test_atr_by_hand():
    h, l, c = [10, 12, 13, 12], [9, 10, 11, 10], [9.5, 11, 12, 11]
    out = atr(h, l, c, 2)
    # TR1 = max(2, 2.5, 0.5) = 2.5 ; TR2 = max(2, 2, 0) = 2 -> seed 2.25 ; TR3 = max(2, 0, 2) = 2 -> (2.25 + 2) / 2
    assert out[2] == pytest.approx(2.25) and out[3] == pytest.approx(2.125)


# ---------- breakout ----------

def breakout_series(vol_mult=2.0, close=103.0, low=101.5, high=103.5):
    n = 30
    closes = [100.0] * (n - 1) + [close]
    highs = [101.0] * (n - 1) + [high]
    lows = [99.0] * (n - 1) + [low]
    vols = [1_000_000] * (n - 1) + [int(1_000_000 * vol_mult)]
    return series(closes, highs, lows, None, vols)


def test_breakout_qualifies_with_stop_below_level():
    p = RuleParams(strategy="breakout", hold_sessions=10)
    ev = status(breakout_series(), p)
    assert ev["status"] == "QUALIFIES", [(r["id"], r["result"]) for r in ev["rules"]]
    assert ev["facts"]["breakout_level"]["value"] == 101.0
    assert ev["suggested_stop"] == round(101.0 * 0.99, 2)
    assert ev["plan"]["exit_date"] > ev["plan"]["entry_date"] and len(ev["plan"]["sessions"]) == 10


def test_breakout_near_miss_on_light_volume():
    ev = status(breakout_series(vol_mult=1.2), RuleParams(strategy="breakout"))
    assert ev["status"] == "NEAR_MISS" and ev["failed"] == ["heavier_volume"]


def test_breakout_weak_close_fails():
    ev = status(breakout_series(close=101.6, low=101.5, high=103.5), RuleParams(strategy="breakout"))
    assert "strong_close" in ev["failed"]


# ---------- mean reversion ----------

def test_mean_reversion_oversold_in_uptrend():
    closes = [100 + i * 0.2 for i in range(220)] + [142.0, 140.5, 139.0]
    ev = status(series(closes), RuleParams(strategy="mean_reversion", **STRATEGIES["mean_reversion"]["defaults"]))
    r = {x["id"]: x for x in ev["rules"]}
    assert r["uptrend"]["result"] == "pass" and r["oversold"]["result"] == "pass", ev["rules"]
    assert ev["status"] == "QUALIFIES" and ev["facts"]["rsi"]["value"] < 10
    assert ev["suggested_stop"] == round(139.0 * 0.99 * (1 - 0.03), 2)


def test_mean_reversion_needs_uptrend():
    closes = [200 - i * 0.2 for i in range(220)] + [155.0, 154.0, 153.0]
    ev = status(series(closes), RuleParams(strategy="mean_reversion"))
    assert "uptrend" in ev["failed"]


def test_mean_reversion_short_history_is_unknown():
    ev = status(series([100.0] * 50), RuleParams(strategy="mean_reversion"))
    assert ev["status"] == "NEEDS_REVIEW"


# ---------- trend following ----------

def test_trend_cross_only_on_the_day_and_atr_stop():
    closes = [100 - i * 0.3 for i in range(60)] + [82 + i * 1.2 for i in range(40)]
    s = series(closes)
    p = RuleParams(strategy="trend_following")
    fe, sl = s.ema(20), s.sma(50)
    cross_days = [i for i in range(1, len(closes)) if None not in (fe[i], sl[i], fe[i - 1], sl[i - 1])
                  and fe[i - 1] <= sl[i - 1] and fe[i] > sl[i]]
    assert len(cross_days) == 1
    i = cross_days[0]
    ev = evaluate_at(s, i, p, "2027-12-31", [])
    r = {x["id"]: x for x in ev["rules"]}
    assert r["cross_up"]["result"] == "pass"
    assert ev["suggested_stop"] == round(s.close[i] - 2.0 * s.atr(14)[i], 2)
    later = evaluate_at(s, i + 1, p, "2027-12-31", [])
    assert {x["id"]: x for x in later["rules"]}["cross_up"]["margin"] == "already above"


# ---------- post-earnings drift ----------

def drift_series(gap=0.05, close_vs_open=1.01, vol_mult=3.0):
    n = 30
    closes = [100.0] * (n - 1)
    o = 100.0 * (1 + gap)
    closes.append(o * close_vs_open)
    opens = closes[:-1] + [o]
    highs = [c * 1.01 for c in closes[:-1]] + [max(o, closes[-1]) * 1.01]
    lows = [c * 0.99 for c in closes[:-1]] + [min(o, closes[-1]) * 0.99]
    vols = [1_000_000] * (n - 1) + [int(1_000_000 * vol_mult)]
    return series(closes, highs, lows, opens, vols)


def test_drift_qualifies_after_report():
    s = drift_series()
    p = RuleParams(strategy="earnings_drift", hold_sessions=10)
    ev = evaluate_at(s, len(s.dates) - 1, p, "2027-12-31", [s.dates[-2]])   # reported after the previous close
    assert ev["status"] == "QUALIFIES", [(r["id"], r["result"]) for r in ev["rules"]]
    assert ev["facts"]["gap"]["value"] == pytest.approx(5.0, abs=0.01)


def test_drift_without_report_or_faded_gap():
    s = drift_series()
    p = RuleParams(strategy="earnings_drift")
    assert "recent_report" in evaluate_at(s, len(s.dates) - 1, p, "2027-12-31", [])["failed"]
    s2 = drift_series(close_vs_open=0.98)
    assert "held_gap" in evaluate_at(s2, len(s2.dates) - 1, p, "2027-12-31", [s2.dates[-1]])["failed"]


# ---------- parsing ----------

@pytest.mark.parametrize("sid", list(STRATEGIES))
def test_templates_parse_to_their_strategy(sid):
    out = parse_strategy(STRATEGIES[sid]["template"])
    assert out["params"]["strategy"] == sid
    assert out["params"]["hold_sessions"] == STRATEGIES[sid]["defaults"]["hold_sessions"]
    assert any("strategy" in i["phrase"] for i in out["interpretations"])


@pytest.mark.parametrize("text,expect", [
    ("Buy breakouts above the 30-day high on 2x average volume, hold 15 sessions", {"strategy": "breakout", "breakout_lookback": 30, "volume_ratio_min": 2.0, "hold_sessions": 15}),
    ("52-week high breakout", {"strategy": "breakout", "breakout_lookback": 260}),
    ("Oversold stocks with RSI(3) below 5 above the 150-day moving average", {"strategy": "mean_reversion", "rsi_period": 3, "rsi_max": 5.0, "trend_sma": 150}),
    ("10/30 moving average crossover with a 3 ATR stop", {"strategy": "trend_following", "fast_ema": 10, "slow_sma": 30, "atr_mult": 3.0}),
    ("Stocks that gap up 6% after earnings on 4x volume", {"strategy": "earnings_drift", "gap_min_pct": 6.0, "volume_ratio_min": 4.0}),
    ("pulled back to the 21-day EMA on lighter volume", {"strategy": "ema_pullback", "ema_period": 21}),
])
def test_parser_reads_numbers_per_strategy(text, expect):
    p = parse_strategy(text)["params"]
    assert {k: p[k] for k in expect} == expect


def test_bad_strategy_rejected():
    with pytest.raises(ValueError):
        RuleParams.from_dict({"strategy": "martingale"})
    with pytest.raises(ValueError):
        RuleParams.from_dict({"strategy": "trend_following", "fast_ema": 60, "slow_sma": 50})


# ---------- consistency: screener, flags and replay share one code path ----------

def _demo(sym):
    rows, earnings = demo_data.generate()
    rows = [r for r in rows if r["symbol"] == sym]
    s = Series(sym, [r["date"] for r in rows], *[[r[k] for r in rows] for k in ("open", "high", "low", "close", "volume")], "demo")
    past = [e["date"] for e in earnings if e["symbol"] == sym and e["kind"] == "past"]
    return s, past


@pytest.mark.parametrize("sid", list(STRATEGIES))
def test_flags_match_evaluator_for_every_strategy(sid):
    s, past = _demo("COIN")
    p = RuleParams(strategy=sid, **STRATEGIES[sid]["defaults"])
    flags = signal_flags(s, p, past)
    core = STRATEGIES[sid]["core"]
    for i in range(0, len(s.dates), 7):
        ev = evaluate_at(s, i, p, None, past)
        assert flags[i] == all(r["result"] == "pass" for r in ev["rules"] if r["id"] in core)


@pytest.mark.parametrize("sid", list(STRATEGIES))
def test_replay_runs_for_every_strategy(sid):
    series_map = {sym: _demo(sym)[0] for sym in demo_data.TICKERS}
    earn = {sym: _demo(sym)[1] for sym in demo_data.TICKERS}
    out = backtest.run(series_map, RuleParams(strategy=sid, **STRATEGIES[sid]["defaults"]), earn)
    assert out["assumptions"]["strategy"] == STRATEGIES[sid]["name"]
    for t in out["trades"]:
        assert t["entry_date"] > t["signal_date"] and t["stop"] < t["entry"]


def test_api_breakout_ruleset_end_to_end(client):
    d = client.post("/api/strategy/parse", json={"text": STRATEGIES["breakout"]["template"]}).json()
    assert d["params"]["strategy"] == "breakout"
    rs = client.post("/api/rulesets", json={"text": d["source_text"], "params": d["params"], "interpretations": d["interpretations"]}).json()
    assert rs["name"] == "Breakout v1"
    res = client.get("/api/watchlist/screen").json()["results"]
    ids = {r["id"] for r in res[0]["rules"]}
    assert {"new_high", "heavier_volume", "strong_close"} <= ids and "pullback_touch" not in ids
    ch = client.get("/api/symbols/NVDA/chart").json()
    assert ch["strategy"] == "breakout" and ch["overlays"][0]["name"] == "20-day high"
    for level in ("beginner", "analyst"):
        ex = client.get(f"/api/symbols/NVDA/explain?level={level}").json()["explanation"]
        assert len(ex["lines"]) >= 5
    assert client.get("/api/backtest").status_code == 200
    assert client.get("/api/forward").status_code == 200
