"""Day trading: intraday indicators, strategies, no-lookahead replay, paper execution, PDT and live quotes."""
from datetime import date

import pytest

from app import db, intraday
from app.intraday import DayParams, bar_starts, evaluate, opening_range, rvol_at, vwap
from test_live_forward import live, world  # noqa: F401  (fixtures)

DAY = "2026-11-19"


def baseline_vol(k):          # U-shaped intraday volume curve
    return 100_000 * (3 if k < 6 else 1 if k < 66 else 2)


def path(kind, k):
    """Price at bar k (0 = 09:30) for the scenario day."""
    if kind == "reclaim_then_target":        # dips below VWAP, reclaims at the 10:25 bar (ends 10:30), then rallies
        if k < 6:
            return 100 - k * 0.4              # 100 -> 98
        if k < 11:
            return 98.0
        return 99.4 + (k - 11) * 0.25         # 10:25 bar closes 99.4 (above VWAP), then climbs
    if kind == "reclaim_then_stop":
        if k < 6:
            return 100 - k * 0.4
        if k < 11:
            return 98.0
        if k == 11:
            return 99.4
        return 95.0                           # collapses after entry
    if kind == "reclaim_then_flat":
        if k < 6:
            return 100 - k * 0.4
        if k < 11:
            return 98.0
        return 99.4 + (0.02 if k % 2 else 0)  # drifts sideways all afternoon
    return 100.0


def bars_for(sym, d, kind):
    out = []
    prev = None
    for k, t in enumerate(bar_starts(d)):
        c = path(kind, k) if d.isoformat() == DAY else 100.0
        o = prev if prev is not None else c
        vol = baseline_vol(k) * (3 if (d.isoformat() == DAY and kind != "flat") else 1)
        out.append({"ts": t.strftime("%Y-%m-%dT%H:%M"), "day": d.isoformat(), "open": o, "high": max(o, c) + 0.05,
                    "low": min(o, c) - 0.05, "close": c, "volume": vol, "source": "test"})
        prev = c
    return out


@pytest.fixture
def scenario(monkeypatch):
    state = {"kind": "reclaim_then_target"}

    def fake_fetch(symbols, start, end, feed):
        out = {}
        for sym in symbols:
            rows = []
            d = start
            while d <= end:
                from app.tradingcal import is_session
                if is_session(d):
                    rows += bars_for(sym, d, state["kind"] if sym == "NVDA" else "flat")
                d = date.fromordinal(d.toordinal() + 1)
            out[sym] = rows
        return out
    monkeypatch.setattr(intraday, "FETCH", fake_fetch)
    return state


# ---------- indicators ----------

def test_vwap_rvol_opening_range_by_hand():
    bars = [{"high": 11, "low": 9, "close": 10, "volume": 100}, {"high": 13, "low": 11, "close": 12, "volume": 300}]
    assert vwap(bars) == [10.0, pytest.approx((10 * 100 + 12 * 300) / 400)]
    priors = [[{"volume": 50}, {"volume": 50}], [{"volume": 150}, {"volume": 150}]]
    rv, base, n = rvol_at([{"volume": 200}, {"volume": 200}], priors, 1)
    assert (rv, base, n) == (pytest.approx(400 / 200), 200, 2)    # cumulative 400 vs avg cumulative (100+300)/2
    hi, lo, n = opening_range([{"high": 5, "low": 4}, {"high": 6, "low": 3}, {"high": 7, "low": 5}], 10)
    assert (hi, lo, n) == (6, 3, 2)


def test_session_has_78_five_minute_bars():
    b = bar_starts(date(2026, 11, 19))
    assert len(b) == 78 and b[0].strftime("%H:%M") == "09:30" and b[-1].strftime("%H:%M") == "15:55"


# ---------- strategies on constructed bars ----------

def _eval(kind, cursor, p=None):
    d = date.fromisoformat(DAY)
    today = [b for b in bars_for("NVDA", d, kind) if intraday.end_of(b["ts"]) <= cursor]
    priors = [bars_for("NVDA", x, "flat") for x in intraday.prior_sessions(d, 10)]
    return evaluate("NVDA", today, priors, p or DayParams(), None, "replay", cursor, DAY)


def test_vwap_reclaim_signals_on_the_reclaim_bar_only():
    ev = _eval("reclaim_then_target", f"{DAY}T10:30")
    assert ev["status"] == "QUALIFIES", [(r["id"], r["result"]) for r in ev["rules"]]
    r = {x["id"]: x for x in ev["rules"]}
    assert r["spread"]["result"] == "info" and "replay" in r["spread"]["observed"]       # not counted in replay
    assert ev["suggested_stop"] < ev["entry_assumption"] < ev["target"]
    assert ev["target"] == round(ev["entry_assumption"] + 2 * (ev["entry_assumption"] - ev["suggested_stop"]), 2)
    later = _eval("reclaim_then_target", f"{DAY}T10:35")
    assert {x["id"]: x for x in later["rules"]}["vwap_reclaim"]["result"] == "fail"       # already above: no new signal


def test_outside_window_is_not_a_signal():
    ev = _eval("reclaim_then_target", f"{DAY}T10:30", DayParams(window_start="11:00"))
    assert "in_window" in ev["failed"]


def test_orb_first_break_only():
    p = DayParams(strategy="orb", or_minutes=15)
    d = date.fromisoformat(DAY)
    bars = bars_for("NVDA", d, "flat")
    for i, b in enumerate(bars):
        b["volume"] *= 3
        if i == 3:                     # 09:45 bar breaks above the 09:30-09:45 range
            b["close"], b["high"] = 101.0, 101.1
        if i == 4:
            b["close"] = 101.2
    priors = [bars_for("NVDA", x, "flat") for x in intraday.prior_sessions(d, 10)]
    ev = evaluate("NVDA", bars[:4], priors, p, None, "replay", f"{DAY}T09:50", DAY)
    assert ev["status"] == "QUALIFIES", ev["rules"]
    hi, lo = ev["facts"]["opening_range"]["value"]["high"], ev["facts"]["opening_range"]["value"]["low"]
    assert ev["suggested_stop"] == round((hi + lo) / 2 * (1 - 0.001), 2)
    ev2 = evaluate("NVDA", bars[:5], priors, p, None, "replay", f"{DAY}T09:55", DAY)
    assert "already broke out" in {x["id"]: x for x in ev2["rules"]}["or_breakout"]["observed"]


def test_live_mode_counts_spread_and_quote_age():
    d = date.fromisoformat(DAY)
    today = [b for b in bars_for("NVDA", d, "reclaim_then_target") if intraday.end_of(b["ts"]) <= f"{DAY}T10:30"]
    priors = [bars_for("NVDA", x, "flat") for x in intraday.prior_sessions(d, 10)]
    wide = {"bid": 99.0, "ask": 99.8, "spread_bps": 80.5, "age_s": 2}
    ev = evaluate("NVDA", today, priors, DayParams(), wide, "live", f"{DAY}T10:30", DAY)
    assert "spread" in ev["failed"] and ev["entry_assumption"] == 99.8          # live entry uses the ask
    stale = {"bid": 99.39, "ask": 99.41, "spread_bps": 2.0, "age_s": 120}
    assert "quote_fresh" in evaluate("NVDA", today, priors, DayParams(), stale, "live", f"{DAY}T10:30", DAY)["failed"]


# ---------- replay + paper execution through the API ----------

def start_replay(c, kind_state=None):
    assert c.put("/api/day/rules", json={"params": {"strategy": "vwap_reclaim"}}).status_code == 200
    r = c.put("/api/day/session", json={"mode": "replay", "date": DAY})
    assert r.status_code == 200, r.text
    return r.json()


def step_to(c, hhmm):
    cur = c.get("/api/day/state").json()["cursor"]
    while cur[11:] < hhmm:
        cur = c.post("/api/day/step", json={"bars": 1}).json()["cursor"]
    return cur


def nvda(c):
    return next(r for r in c.get("/api/day/screen").json()["results"] if r["symbol"] == "NVDA")


def test_replay_has_no_lookahead(live, scenario):
    st = start_replay(live)
    assert st["cursor"] == f"{DAY}T09:35"
    ch = live.get("/api/day/chart/NVDA").json()
    assert len(ch["bars"]) == 1 and ch["bars"][-1]["ts"] == f"{DAY}T09:30"
    step_to(live, "10:00")
    assert len(live.get("/api/day/chart/NVDA").json()["bars"]) == 6


def test_day_trade_to_target(live, scenario):
    start_replay(live)
    step_to(live, "10:30")
    assert nvda(live)["status"] == "QUALIFIES"
    pv = live.post("/api/day/preview", json={"symbol": "NVDA"}).json()
    assert pv["style"] == "intraday" and pv["flat_by"] == "15:55" and pv["est_loss_if_stopped"] <= pv["risk_budget"] + 1
    r = live.post("/api/day/orders", json={"preview_id": pv["id"], "confirm": True})
    assert r.status_code == 200, r.text
    assert live.post("/api/day/orders", json={"preview_id": pv["id"], "confirm": True}).status_code == 409   # duplicate
    live.post("/api/day/step", json={"bars": 1})
    pos = next(p for p in live.get("/api/day/positions").json() if p["symbol"] == "NVDA")
    first_open = next(b for b in bars_for("NVDA", date.fromisoformat(DAY), "reclaim_then_target") if b["ts"] == f"{DAY}T10:30")["open"]
    assert pos["held"] == pv["qty"] and pos["entry"] == pytest.approx(first_open * 1.0002, abs=1e-3)   # next bar open + 2 bps
    step_to(live, "16:00")
    j = next(x for x in live.get("/api/journal").json() if x["symbol"] == "NVDA")
    assert j["status"] == "closed" and j["exit_reason"] == "target" and j["realized_pnl"] > 0
    assert j["plan"]["style"] == "intraday" and j["adherence"]["followed"] is True


def test_day_trade_stop(live, scenario):
    scenario["kind"] = "reclaim_then_stop"
    start_replay(live)
    step_to(live, "10:30")
    pv = live.post("/api/day/preview", json={"symbol": "NVDA"}).json()
    live.post("/api/day/orders", json={"preview_id": pv["id"], "confirm": True})
    step_to(live, "11:00")
    j = next(x for x in live.get("/api/journal").json() if x["symbol"] == "NVDA")
    assert j["exit_reason"] == "stop" and j["realized_pnl"] < 0


def test_day_trade_flat_by_close(live, scenario):
    scenario["kind"] = "reclaim_then_flat"
    start_replay(live)
    step_to(live, "10:30")
    pv = live.post("/api/day/preview", json={"symbol": "NVDA"}).json()
    live.post("/api/day/orders", json={"preview_id": pv["id"], "confirm": True})
    step_to(live, "16:00")
    j = next(x for x in live.get("/api/journal").json() if x["symbol"] == "NVDA")
    assert j["exit_reason"] == "flat" and j["exit_date"] == DAY


def test_manual_flatten_next_bar(live, scenario):
    scenario["kind"] = "reclaim_then_flat"
    start_replay(live)
    step_to(live, "10:30")
    pv = live.post("/api/day/preview", json={"symbol": "NVDA"}).json()
    oid = live.post("/api/day/orders", json={"preview_id": pv["id"], "confirm": True}).json()["order"]["id"]
    step_to(live, "10:40")
    assert live.post(f"/api/day/orders/{oid}/flatten").status_code == 200
    step_to(live, "10:45")
    j = next(x for x in live.get("/api/journal").json() if x["symbol"] == "NVDA")
    assert j["exit_reason"] == "manual_exit" and j["adherence"]["followed"] is False


def test_stale_preview_after_a_new_bar(live, scenario):
    start_replay(live)
    step_to(live, "10:30")
    pv = live.post("/api/day/preview", json={"symbol": "NVDA"}).json()
    live.post("/api/day/step", json={"bars": 1})
    assert live.post("/api/day/orders", json={"preview_id": pv["id"], "confirm": True}).status_code == 409


def test_pdt_hard_block_for_small_accounts(live, scenario):
    conn = db.connect()
    db.kv_set(conn, "account", {"starting_equity": 20000.0, "cash": 20000.0})
    for i, d in enumerate(["2026-11-17", "2026-11-18", "2026-11-19"]):        # three earlier day trades
        for side in ("buy", "sell"):
            oid = f"pdt{i}{side}"
            conn.execute("""INSERT INTO orders(id,client_order_id,broker,symbol,side,qty,order_type,purpose,status,filled_qty,created_at,updated_at)
                            VALUES(?,?,'simulator','COIN',?,1,'market','entry','filled',1,'x','x')""", (oid, oid, side))
            conn.execute("INSERT INTO fills(order_id,session_date,qty,price) VALUES(?,?,1,100)", (oid, d))
    conn.close()
    start_replay(live)
    step_to(live, "10:30")
    pv = live.post("/api/day/preview", json={"symbol": "NVDA"}).json()
    pdt = next(c for c in pv["risk"]["checks"] if c["id"] == "pdt")
    assert pdt["kind"] == "hard" and pdt["result"] == "block"
    r = live.post("/api/day/orders", json={"preview_id": pv["id"], "confirm": True, "risk_overrides": {"pdt": "I know what I'm doing"}})
    assert r.status_code == 422


def test_swing_simulator_ignores_day_trades(live, scenario):
    start_replay(live)
    step_to(live, "10:30")
    pv = live.post("/api/day/preview", json={"symbol": "NVDA"}).json()
    oid = live.post("/api/day/orders", json={"preview_id": pv["id"], "confirm": True}).json()["order"]["id"]
    conn = db.connect()
    from app.brokers import simulator as sim
    from app.trading import plan_exits
    assert oid not in plan_exits(conn)
    sim.process_session(conn, "2026-11-23", {"NVDA": {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1e6}}, {})
    assert conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()["status"] == "pending"
    conn.close()


def test_replay_requires_past_session_and_live_data(client, live):
    assert live.put("/api/day/session", json={"mode": "replay", "date": "2026-11-21"}).status_code == 400   # Saturday


def test_day_rules_validation(live):
    assert live.put("/api/day/rules", json={"params": {"window_start": "15:40", "window_end": "15:30"}}).status_code == 400
    v1 = live.put("/api/day/rules", json={"params": {"strategy": "orb"}}).json()
    assert v1["version"] == 1 and v1["params"]["strategy"] == "orb"
