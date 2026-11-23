import pytest

from app import backtest, demo_data
from app.indicators import ema, trailing_mean
from app.rules import RuleParams, Series, evaluate_at, parse_strategy, signal_flags


# ---------- indicator math ----------

def test_ema_seed_and_recursion_match_hand_calculation():
    vals = [10, 11, 12, 13, 14.0]
    out = ema(vals, 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(11.0)                 # SMA seed of 10, 11, 12
    assert out[3] == pytest.approx(0.5 * 13 + 0.5 * 11)  # alpha = 2/(3+1) = 0.5
    assert out[4] == pytest.approx(0.5 * 14 + 0.5 * 12)


def test_ema_missing_value_breaks_chain_and_reseeds():
    out = ema([1, 2, 3, None, 4, 5, 6.0], 3)
    assert out[3] is None and out[4] is None and out[5] is None
    assert out[6] == pytest.approx(5.0)


def test_trailing_mean_excludes_current_session_and_needs_full_window():
    v = [1, 2, 3, 4, 100.0]
    assert trailing_mean(v, 4, 4) == pytest.approx(2.5)   # 100 (session 4) excluded
    assert trailing_mean(v, 2, 4) is None                 # not enough history
    assert trailing_mean([1, None, 3, 4, 5], 4, 4) is None


# ---------- demo cases ----------

def _series(sym):
    rows, earnings = demo_data.generate()
    rows = [r for r in rows if r["symbol"] == sym and r["date"] <= demo_data.AS_OF.isoformat()]
    s = Series(sym, [r["date"] for r in rows], *[[r[k] for r in rows] for k in ("open", "high", "low", "close", "volume")], demo_data.SOURCE)
    nxt = next(e["date"] for e in earnings if e["symbol"] == sym and e["kind"] == "next")
    past = [e["date"] for e in earnings if e["symbol"] == sym and e["kind"] == "past"]
    return s, nxt, past


@pytest.mark.parametrize("sym,status", [("NVDA", "QUALIFIES"), ("SNDK", "NEAR_MISS"), ("IREN", "NEEDS_REVIEW"), ("COIN", "NO_SETUP")])
def test_demo_cases(sym, status):
    s, nxt, past = _series(sym)
    ev = evaluate_at(s, len(s.dates) - 1, RuleParams(), nxt, past)
    assert ev["status"] == status


def test_near_miss_reports_exact_reason():
    s, nxt, past = _series("SNDK")
    ev = evaluate_at(s, len(s.dates) - 1, RuleParams(), nxt, past)
    assert ev["failed"] == ["lighter_volume"]
    vol = next(r for r in ev["rules"] if r["id"] == "lighter_volume")
    assert vol["observed"] == "86% of avg" and "pts vs threshold" in vol["margin"]
    assert next(r for r in ev["rules"] if r["id"] == "earnings")["result"] == "warn"


def test_earnings_mode_exclude_turns_warning_into_a_failure():
    s, nxt, past = _series("SNDK")
    p = RuleParams(volume_ratio_max=0.9)
    assert evaluate_at(s, len(s.dates) - 1, p, nxt, past)["status"] == "QUALIFIES_WITH_WARNING"
    p.earnings_mode = "exclude"
    ev = evaluate_at(s, len(s.dates) - 1, p, nxt, past)
    assert ev["status"] == "NEAR_MISS" and ev["failed"] == ["earnings"]


def test_threshold_change_moves_nvda_to_near_miss():
    s, nxt, past = _series("NVDA")
    ev = evaluate_at(s, len(s.dates) - 1, RuleParams(volume_ratio_max=0.6), nxt, past)
    assert ev["status"] == "NEAR_MISS" and ev["failed"] == ["lighter_volume"]


# ---------- missing data ----------

def test_missing_earnings_date_is_unknown_not_clear():
    s, _, past = _series("NVDA")
    ev = evaluate_at(s, len(s.dates) - 1, RuleParams(), None, past)
    assert next(r for r in ev["rules"] if r["id"] == "earnings")["result"] == "unknown"
    assert ev["status"] == "NEEDS_REVIEW"


def test_missing_volume_in_lookback_makes_volume_rule_unknown():
    s, nxt, past = _series("NVDA")
    s.volume[-5] = None
    ev = evaluate_at(s, len(s.dates) - 1, RuleParams(), nxt, past)
    assert next(r for r in ev["rules"] if r["id"] == "lighter_volume")["result"] == "unknown"
    assert ev["status"] == "NEEDS_REVIEW"


def test_missing_close_makes_price_rules_unknown():
    s, nxt, past = _series("NVDA")
    s.close[-3] = None   # breaks the EMA chain for the following sessions
    ev = evaluate_at(s, len(s.dates) - 1, RuleParams(), nxt, past)
    assert {r["id"]: r["result"] for r in ev["rules"]}["pullback_touch"] == "unknown"
    assert ev["status"] == "NEEDS_REVIEW"


def test_insufficient_history():
    s, nxt, past = _series("NVDA")
    short = s.truncated(10)
    ev = evaluate_at(short, 10, RuleParams(), nxt, past)
    assert ev["rules"][0]["result"] == "unknown" and ev["status"] == "NEEDS_REVIEW"


# ---------- signal timing / no look-ahead ----------

def test_evaluation_uses_no_future_bars():
    s, nxt, past = _series("NVDA")
    p = RuleParams()
    for i in range(30, len(s.dates), 17):
        full = evaluate_at(s, i, p, nxt, past)
        trunc = evaluate_at(s.truncated(i), i, p, nxt, past)
        assert [r["result"] for r in full["rules"]] == [r["result"] for r in trunc["rules"]]
        assert full["facts"]["ema"]["value"] == trunc["facts"]["ema"]["value"]


def test_signal_flags_agree_with_evaluator():
    s, nxt, past = _series("COIN")
    p = RuleParams()
    flags = signal_flags(s, p)
    for i in range(25, len(s.dates)):
        ev = evaluate_at(s, i, p, nxt, past)
        core = all(r["result"] == "pass" for r in ev["rules"] if r["id"] in ("pullback_touch", "close_above", "lighter_volume"))
        assert flags[i] == core


def test_replay_enters_next_open_never_signal_day():
    s, _, _ = _series("NVDA")
    trades, incomplete = backtest.trades_for(s, RuleParams(), [])
    assert trades
    for t in trades:
        i = s.dates.index(t["signal_date"])
        assert t["entry_date"] == s.dates[i + 1]
        assert t["entry"] == pytest.approx(s.open[i + 1] * 1.0005, rel=1e-6)
        assert t["exit_date"] > t["signal_date"]
    # The final bar's signal cannot be traded: its exit is beyond the data.
    assert incomplete >= 1


def test_replay_never_overlaps_trades_in_one_ticker():
    s, _, _ = _series("IREN")
    trades, _ = backtest.trades_for(s, RuleParams(), [])
    for a, b in zip(trades, trades[1:]):
        assert b["signal_date"] >= a["exit_date"]


def test_small_sample_warning():
    s, _, _ = _series("NVDA")
    out = backtest.run({"NVDA": s}, RuleParams(), {})
    assert out["stats"]["trades"] < backtest.MIN_TRADES
    assert "too few" in out["sample_warning"]


# ---------- strategy parsing ----------

def test_parser_defaults_for_example_sentence():
    out = parse_strategy("Find stocks on my watchlist that pulled back to the 9-day EMA on lighter volume. "
                         "Flag any company reporting earnings before my planned exit.")
    p = out["params"]
    assert (p["ema_period"], p["volume_ratio_max"], p["volume_lookback"], p["hold_sessions"], p["earnings_mode"]) == (9, 0.8, 20, 5, "warn")
    phrases = " ".join(i["phrase"] for i in out["interpretations"])
    assert "lighter volume" in phrases and "planned exit" in phrases and "earnings" in phrases


def test_parser_reads_user_numbers():
    p = parse_strategy("Pullback to the 21-day EMA with volume below 70% of the 50-day average volume, "
                       "hold for 10 sessions, exclude anything with earnings.")["params"]
    assert (p["ema_period"], p["volume_ratio_max"], p["volume_lookback"], p["hold_sessions"], p["earnings_mode"]) == (21, 0.7, 50, 10, "exclude")


def test_invalid_params_rejected():
    with pytest.raises(ValueError):
        RuleParams.from_dict({"ema_period": 1})
