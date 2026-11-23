"""Live mode and forward test, with yfinance replaced by a fake fed from the reproducible demo bars.

The fake 'Yahoo' serves bars up to a movable server date (including a partial bar for
'today'), so we can check completed-bar filtering, incremental paper fills on new
real sessions, and that the forward test never back-fills.
"""
from datetime import datetime

import pandas as pd
import pytest

from app import db, demo_data, live_data
from app.live_data import ET, last_completed_session

ROWS, _ = demo_data.generate()


class FakeTicker:
    def __init__(self, sym, world):
        self.sym, self.world = sym, world

    def history(self, period, interval, auto_adjust):
        if self.sym in self.world["fail"]:
            raise ConnectionError("rate limited")
        rows = [r for r in ROWS if r["symbol"] == self.sym and r["date"] <= self.world["today"]]
        if not rows:
            return pd.DataFrame()
        idx = pd.DatetimeIndex([pd.Timestamp(r["date"]).tz_localize(ET) for r in rows])
        df = pd.DataFrame({"Open": [r["open"] for r in rows], "High": [r["high"] for r in rows], "Low": [r["low"] for r in rows],
                           "Close": [r["close"] for r in rows], "Volume": [r["volume"] for r in rows]}, index=idx)
        for d in self.world["nan_close"].get(self.sym, []):
            df.loc[pd.Timestamp(d).tz_localize(ET), "Close"] = float("nan")
        return df

    def get_earnings_dates(self, limit=12):
        dates = {"NVDA": ["2026-11-18", "2027-02-24"], "SNDK": ["2026-11-24"], "COIN": ["2027-02-11"], "IREN": []}[self.sym]
        if not dates:
            return pd.DataFrame()
        return pd.DataFrame({"EPS Estimate": [1.0] * len(dates)},
                            index=pd.DatetimeIndex([pd.Timestamp(d + " 16:00").tz_localize(ET) for d in dates]))

    calendar = {}

    @property
    def fast_info(self):
        q = self.world.get("quote", {}).get(self.sym)
        if q is None:
            raise AttributeError("no quote")
        return {"lastPrice": q}


class FakeYF:
    def __init__(self, world):
        self.world = world

    def Ticker(self, sym):
        return FakeTicker(sym, self.world)


@pytest.fixture
def world(monkeypatch):
    w = {"today": "2026-11-20", "clock": datetime(2026, 11, 20, 17, 0, tzinfo=ET), "fail": set(), "nan_close": {}}
    monkeypatch.setattr(live_data, "_yf", FakeYF(w))
    monkeypatch.setattr(live_data, "now_et", lambda: w["clock"])
    return w


def go_to(world, day, hour=17):
    world["today"] = day
    y, m, d = map(int, day.split("-"))
    world["clock"] = datetime(y, m, d, hour, 0, tzinfo=ET)


@pytest.fixture
def live(client, world):
    r = client.put("/api/data/mode", json={"mode": "live"})
    assert r.status_code == 200, r.text
    yield client
    db.set_data_mode("demo")


def confirm(c):
    d = c.get("/api/rulesets").json()["draft"]
    assert c.post("/api/rulesets", json={"text": d["source_text"], "params": d["params"], "interpretations": d["interpretations"]}).status_code == 200


# ---------- completed-bar logic ----------

@pytest.mark.parametrize("clock,expected", [
    (datetime(2026, 11, 20, 15, 0, tzinfo=ET), "2026-11-19"),    # Friday, market still open
    (datetime(2026, 11, 20, 16, 30, tzinfo=ET), "2026-11-20"),   # Friday after the close
    (datetime(2026, 11, 22, 11, 0, tzinfo=ET), "2026-11-20"),    # Sunday
    (datetime(2026, 11, 27, 9, 0, tzinfo=ET), "2026-11-25"),     # day after Thanksgiving, before close
])
def test_last_completed_session(clock, expected):
    assert last_completed_session(clock).isoformat() == expected


def test_partial_bar_for_today_is_never_stored(world):
    go_to(world, "2026-11-23", hour=11)   # Monday 11:00 ET: Yahoo already shows a forming bar
    bars = live_data.fetch_bars("NVDA", world["clock"])
    assert bars[-1]["date"] == "2026-11-20"


def test_missing_close_stored_as_none_not_zero(world):
    world["nan_close"]["SNDK"] = ["2026-11-20"]
    bars = live_data.fetch_bars("SNDK", world["clock"])
    assert bars[-1]["close"] is None


# ---------- live mode through the API ----------

def test_switch_to_live_fetches_and_screens(live):
    s = live.get("/api/status").json()
    assert s["data_mode"] == "live" and s["simulated_data"] is False and s["as_of"] == "2026-11-20"
    assert "yfinance" in s["data_label"]
    sc = {r["symbol"]: r["status"] for r in live.get("/api/watchlist/screen").json()["results"]}
    assert sc["NVDA"] == "QUALIFIES" and sc["IREN"] == "NEEDS_REVIEW"   # IREN: no future earnings from 'Yahoo'


def test_provider_gap_becomes_needs_review_with_reason(live, world):
    world["nan_close"]["NVDA"] = ["2026-11-20"]
    live.post("/api/live/refresh")
    ev = next(r for r in live.get("/api/watchlist/screen").json()["results"] if r["symbol"] == "NVDA")
    assert ev["status"] == "NEEDS_REVIEW"
    assert "provider gap" in ev["rules"][0]["margin"]


def test_failed_fetch_keeps_cache_and_reports(live, world):
    world["fail"].add("COIN")
    go_to(world, "2026-11-23")
    r = live.post("/api/live/refresh").json()
    assert r["report"]["COIN"]["ok"] is False and "rate limited" in r["report"]["COIN"]["error"]
    ev = next(x for x in live.get("/api/watchlist/screen").json()["results"] if x["symbol"] == "COIN")
    assert ev["date"] == "2026-11-20" and ev["rules"][0]["result"] == "unknown"   # stale, so not trusted
    assert "stale" in ev["rules"][0]["margin"]


def test_demo_controls_blocked_in_live(live):
    assert live.post("/api/sim/advance").status_code == 409


def test_watchlist_editing(live, client):
    assert live.put("/api/watchlist", json={"symbols": ["NVDA", "bad symbol!"]}).status_code == 400
    r = live.put("/api/watchlist", json={"symbols": ["nvda", "COIN"]}).json()
    assert r["watchlist"] == ["NVDA", "COIN"]


def test_watchlist_fixed_in_demo(client):
    assert client.put("/api/watchlist", json={"symbols": ["AAPL"]}).status_code == 409


def test_paper_order_fills_on_real_next_open(live, world):
    confirm(live)
    pv = live.post("/api/previews", json={"symbol": "NVDA"}).json()
    oid = live.post("/api/orders", json={"preview_id": pv["id"], "confirm": True}).json()["order"]["id"]
    go_to(world, "2026-11-23", hour=12)            # Monday midday: still no completed new bar
    live.post("/api/live/refresh")
    assert next(o for o in live.get("/api/orders").json() if o["id"] == oid)["status"] == "pending"
    go_to(world, "2026-11-23", hour=17)            # after the close: the real bar is final
    live.post("/api/live/refresh")
    o = next(o for o in live.get("/api/orders").json() if o["id"] == oid)
    real_open = next(r["open"] for r in ROWS if r["symbol"] == "NVDA" and r["date"] == "2026-11-23")
    assert o["status"] == "filled" and o["fills"][0]["price"] == pytest.approx(real_open * 1.0005, rel=1e-6)
    go_to(world, "2026-12-01")                      # several sessions arrive in one refresh
    live.post("/api/live/refresh")
    j = live.get("/api/journal").json()[0]
    assert j["status"] == "closed"
    assert j["exit_reason"] == "stop" or (j["exit_reason"] == "plan_exit" and j["exit_date"] == "2026-11-30")


# ---------- forward test ----------

def test_forward_records_once_and_never_backfills(live):
    confirm(live)
    for _ in range(3):
        live.get("/api/watchlist/screen")
    f = live.get("/api/forward").json()
    nvda = [s for s in f["signals"] if s["symbol"] == "NVDA"]
    assert len(nvda) == 1 and nvda[0]["signal_date"] == "2026-11-20"
    assert all(s["signal_date"] == "2026-11-20" for s in f["signals"])   # earlier historical signals not back-filled
    assert f["stats"]["recorded"] == len(f["signals"]) and f["stats"]["waiting_entry"] == len(f["signals"])


def test_forward_follows_signal_to_close(live, world):
    confirm(live)
    live.get("/api/watchlist/screen")
    go_to(world, "2026-11-24")
    live.post("/api/live/refresh")
    sig = next(s for s in live.get("/api/forward").json()["signals"] if s["symbol"] == "NVDA" and s["signal_date"] == "2026-11-20")
    assert sig["state"] in ("open", "closed") and sig["entry_date"] == "2026-11-23"
    go_to(world, "2026-12-02")
    live.post("/api/live/refresh")
    f = live.get("/api/forward").json()
    sig = next(s for s in f["signals"] if s["symbol"] == "NVDA" and s["signal_date"] == "2026-11-20")
    assert sig["state"] == "closed" and sig["return_pct"] is not None and sig["bench_return_pct"] is not None
    assert f["stats"]["closed"] >= 1 and "closed signal" in f["sample_warning"]


def test_forward_frozen_evidence_survives_rule_change(live):
    confirm(live)
    live.get("/api/watchlist/screen")
    d = live.get("/api/rulesets").json()["draft"]
    live.post("/api/rulesets", json={"text": "v2", "params": {**d["params"], "volume_ratio_max": 0.5}, "interpretations": []})
    f = live.get("/api/forward").json()
    assert f["ruleset_version"] == 2 and f["stats"]["recorded"] == 0      # v2 stats start fresh
    old = [s for s in f["signals"] if s["ruleset_version"] == 1]
    assert old and old[0]["evidence"]["status"] == "QUALIFIES"


def test_blank_newest_close_is_filled_from_the_quote(world):
    """Yahoo left SNDK's 2026-11-20 close blank while its quote had 79.24: fill it, label the source."""
    world["nan_close"] = {"SNDK": ["2026-11-20"]}
    world["quote"] = {"SNDK": 79.24}
    bars = live_data.fetch_bars("SNDK")
    assert bars[-1]["date"] == "2026-11-20" and bars[-1]["close"] == 79.24 and "filled from the Yahoo quote" in bars[-1]["source"]
    world["quote"] = {}
    assert live_data.fetch_bars("SNDK")[-1]["close"] is None          # no quote: stays missing, never zero
