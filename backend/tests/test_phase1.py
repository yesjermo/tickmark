"""Phase 1: broker connection, broker-side lifecycle, reconciliation, pre-trade risk engine.

Alpaca is replaced by the stateful fake in fake_alpaca.py; no network calls.
"""
import sqlite3
from datetime import datetime

import pytest

from app import credentials, db, risk, scheduler
from app.brokers import alpaca as alpaca_mod
from app.live_data import ET
from fake_alpaca import GOOD_KEY, GOOD_SECRET, FakeAlpaca
from test_live_forward import confirm, go_to, live, world  # noqa: F401  (fixtures)


def preview(c, sym="NVDA", **ov):
    r = c.post("/api/previews", json={"symbol": sym, "overrides": ov})
    assert r.status_code == 200, r.text
    return r.json()


def submit(c, pv, **kw):
    return c.post("/api/orders", json={"preview_id": pv["id"], "confirm": True, **kw})


def order(c, oid):
    return next(o for o in c.get("/api/orders").json() if o["id"] == oid)


def set_clock(world, day, hour, minute=0):
    go_to(world, day, hour=hour)
    world["clock"] = world["clock"].replace(minute=minute)


@pytest.fixture
def fake(monkeypatch):
    f = FakeAlpaca()
    monkeypatch.setattr(alpaca_mod, "TRANSPORT", f.transport())
    return f


@pytest.fixture
def broker(live, fake):
    assert live.post("/api/broker/connect", json={"key_id": GOOD_KEY, "secret": GOOD_SECRET}).status_code == 200
    assert live.put("/api/broker/mode", json={"mode": "alpaca_paper"}).status_code == 200
    confirm(live)
    return live


def open_filled_position(c, fake, price=168.0):
    pv = preview(c)
    oid = submit(c, pv).json()["order"]["id"]
    fake.fill_entry(pv["id"], price)
    o = order(c, oid)                                     # GET /api/orders syncs from the broker
    assert o["status"] == "filled"
    return pv, oid, o


# ---------- 1. connect-broker screen ----------

def test_connect_rejects_bad_keys_and_saves_nothing(live, fake):
    r = live.post("/api/broker/connect", json={"key_id": "PKWRONG", "secret": "nope"})
    assert r.status_code == 400 and "failed" in r.json()["detail"]
    assert live.get("/api/broker/connection").json()["connected"] is False


def test_connect_saves_to_keychain_and_never_returns_secret(live, fake):
    r = live.post("/api/broker/connect", json={"key_id": GOOD_KEY, "secret": GOOD_SECRET})
    assert r.status_code == 200
    body = r.text
    assert GOOD_SECRET not in body and GOOD_KEY not in body          # only a masked hint leaves the server
    st = r.json()
    assert st["connected"] and st["source"] == "keychain" and st["key_hint"] == "PK…1234"
    assert st["account"]["equity"] == 100000.0
    assert credentials.get_alpaca()[:2] == (GOOD_KEY, GOOD_SECRET)
    d = live.delete("/api/broker/connect").json()
    assert d["connected"] is False and d["broker_mode"] == "simulator"


def test_sizing_uses_broker_equity(broker, fake):
    full = preview(broker)
    fake.account["equity"] = "50000"
    half = preview(broker)
    assert half["equity_source"] == "Alpaca account"
    assert half["qty"] == pytest.approx(full["qty"] / 2, abs=1)


# ---------- 2. broker-side lifecycle ----------

def test_entry_fill_and_stop_leg_are_synced(broker, fake):
    pv, oid, o = open_filled_position(broker, fake)
    assert o["fills"][0]["price"] == 168.0 and o["fills"][0]["qty"] == pv["qty"]
    stop = next(ch for ch in o["children"] if ch["purpose"] == "stop")
    assert stop["status"] == "pending" and stop["stop_price"] == pv["stop_price"]
    j = broker.get("/api/journal").json()[0]
    assert j["entry_price"] == 168.0 and j["status"] == "open"


def test_planned_exit_sends_moc_and_journal_closes(broker, fake, world):
    pv, oid, _ = open_filled_position(broker, fake)
    set_clock(world, pv["plan"]["exit_date"], 15, 40)
    ev = broker.post("/api/broker/exits").json()["events"]
    assert any("planned exit sent" in e for e in ev)
    leg = next(o for o in fake.orders.values() if o["type"] == "stop")
    assert leg["status"] == "canceled"                              # stop canceled first, so shares aren't held
    assert fake.posts[-1]["time_in_force"] == "cls" and fake.posts[-1]["side"] == "sell"
    fake.fill_sells("NVDA", 175.0, tif="cls")
    broker.get("/api/orders")
    j = broker.get("/api/journal").json()[0]
    assert j["status"] == "closed" and j["exit_reason"] == "plan_exit"
    assert j["realized_pnl"] == pytest.approx((175.0 - 168.0) * pv["qty"], abs=0.01)
    assert j["adherence"]["followed"] is True


def test_planned_exit_not_sent_after_moc_cutoff_then_late_market_exit(broker, fake, world):
    pv, oid, _ = open_filled_position(broker, fake)
    set_clock(world, pv["plan"]["exit_date"], 16, 30)
    assert broker.post("/api/broker/exits").json()["events"] == []
    from datetime import date
    from app.tradingcal import next_sessions
    nxt = next_sessions(date.fromisoformat(pv["plan"]["exit_date"]), 1)[0].isoformat()
    set_clock(world, nxt, 10, 0)
    ev = broker.post("/api/broker/exits").json()["events"]
    assert any("late planned exit" in e for e in ev) and fake.posts[-1]["time_in_force"] == "day"


def test_stop_fill_closes_journal(broker, fake):
    pv, oid, _ = open_filled_position(broker, fake)
    fake.trigger_stop("NVDA", pv["stop_price"] - 0.5)
    broker.get("/api/orders")
    j = broker.get("/api/journal").json()[0]
    assert j["status"] == "closed" and j["exit_reason"] == "stop" and j["realized_pnl"] < 0


def test_manual_close_cancels_stop_then_sells(broker, fake):
    pv, oid, _ = open_filled_position(broker, fake)
    r = broker.post(f"/api/orders/{oid}/close")
    assert r.status_code == 200 and r.json()["purpose"] == "manual_exit"
    assert next(o for o in fake.orders.values() if o["type"] == "stop")["status"] == "canceled"
    assert broker.post(f"/api/orders/{oid}/close").status_code == 409          # no duplicate exit
    fake.fill_sells("NVDA", 170.0)
    broker.get("/api/orders")
    j = broker.get("/api/journal").json()[0]
    assert j["exit_reason"] == "manual_exit" and j["adherence"]["followed"] is False


def test_duplicate_submit_still_one_broker_order(broker, fake):
    pv = preview(broker)
    assert submit(broker, pv).status_code == 200
    assert submit(broker, pv).status_code == 409
    assert len([p for p in fake.posts if p["side"] == "buy"]) == 1


# ---------- 3. reconciliation ----------

def test_reconcile_in_sync(broker, fake):
    open_filled_position(broker, fake)
    r = broker.post("/api/reconcile").json()
    assert r["ok"] is True and r["checked"] == 1


def test_reconcile_flags_untracked_and_unprotected(broker, fake):
    open_filled_position(broker, fake)
    fake.add_outside_position("MSFT", 10, 400.0)
    next(o for o in fake.orders.values() if o["type"] == "stop")["status"] = "canceled"   # stop canceled outside the app
    r = broker.post("/api/reconcile").json()
    types = {(b["type"], b["symbol"]) for b in r["breaks"]}
    assert ("untracked_position", "MSFT") in types
    assert ("unprotected_position", "NVDA") in types and ("unprotected_position", "MSFT") in types
    assert broker.get("/api/broker/connection").json()["reconciliation"]["ok"] is False


def test_reconcile_flags_quantity_mismatch(broker, fake):
    pv, _, _ = open_filled_position(broker, fake)
    fake.positions["NVDA"]["qty"] -= 3
    r = broker.post("/api/reconcile").json()
    assert any(b["type"] == "qty_mismatch" for b in r["breaks"])


def test_simulator_reconcile_flags_unprotected(confirmed):
    pv = preview(confirmed)
    oid = submit(confirmed, pv).json()["order"]["id"]
    confirmed.post("/api/sim/advance")
    assert confirmed.post("/api/reconcile").json()["ok"] is True
    stop = next(ch for ch in order(confirmed, oid)["children"] if ch["purpose"] == "stop")
    confirmed.post(f"/api/orders/{stop['id']}/cancel")
    r = confirmed.post("/api/reconcile").json()
    assert any(b["type"] == "unprotected_position" for b in r["breaks"])


# ---------- 4. pre-trade risk engine ----------

def checks(pv):
    return {c["id"]: c for c in pv["risk"]["checks"]}


def test_kill_switch_blocks(confirmed):
    assert confirmed.put("/api/risk/kill-switch", json={"on": True, "reason": "rough week, stepping back"}).status_code == 200
    pv = preview(confirmed)
    assert checks(pv)["kill_switch"]["result"] == "block" and pv["risk"]["blocked"]
    r = submit(confirmed, pv)
    assert r.status_code == 422 and "kill switch" in r.json()["detail"].lower()
    confirmed.put("/api/risk/kill-switch", json={"on": False})
    assert submit(confirmed, preview(confirmed)).status_code == 200


def test_restricted_symbol_is_hard_block(confirmed):
    confirmed.put("/api/risk/limits", json={"params": {"restricted": ["nvda"]}, "note": "MNPI"})
    pv = preview(confirmed)
    assert checks(pv)["restricted"]["result"] == "block"
    assert submit(confirmed, pv, risk_overrides={"restricted": "I really want to buy it"}).status_code == 422


def test_soft_block_needs_reason_and_is_logged(confirmed):
    lim = confirmed.put("/api/risk/limits", json={"params": {"max_portfolio_heat_pct": 0.1}, "note": "tight"}).json()
    pv = preview(confirmed)
    assert checks(pv)["portfolio_heat"]["result"] == "override" and pv["risk"]["limits_version"] == lim["version"]
    assert submit(confirmed, pv).status_code == 422
    assert submit(confirmed, pv, risk_overrides={"portfolio_heat": "short"}).status_code == 422
    r = submit(confirmed, pv, risk_overrides={"portfolio_heat": "Single position; heat limit is for multi-name books"})
    assert r.status_code == 200
    log = confirmed.get("/api/risk").json()["overrides"]
    assert log[0]["check_id"] == "portfolio_heat" and log[0]["client_order_id"] == pv["id"]
    j = confirmed.get("/api/journal").json()[0]
    assert j["plan"]["risk_overrides"][0]["check"] == "portfolio_heat"
    for _ in range(7):
        confirmed.post("/api/sim/advance")
    j = confirmed.get("/api/journal").json()[0]
    risk_check = next(c for c in j["adherence"]["checks"] if c["check"].startswith("Passed pre-trade risk"))
    assert risk_check["ok"] is False and "portfolio_heat" in risk_check["detail"]


def test_override_log_is_append_only(confirmed):
    confirmed.put("/api/risk/kill-switch", json={"on": True, "reason": "testing the log"})
    conn = db.connect()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE risk_overrides SET reason='edited'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM risk_overrides")
    conn.close()


def test_limits_are_versioned(confirmed):
    confirmed.put("/api/risk/limits", json={"params": {"max_open_positions": 3}, "note": "a"})
    confirmed.put("/api/risk/limits", json={"params": {"max_open_positions": 4}, "note": "b"})
    h = confirmed.get("/api/risk").json()["history"]
    assert [x["version"] for x in h] == [2, 1] and h[0]["params"]["max_open_positions"] == 4
    assert confirmed.put("/api/risk/limits", json={"params": {"max_open_positions": 0}}).status_code == 400


def test_daily_loss_limit_blocks(confirmed, monkeypatch):
    monkeypatch.setattr(risk, "realized_today", lambda conn, session, broker: -5000.0)
    pv = preview(confirmed)
    assert checks(pv)["daily_loss"]["result"] == "block"


def test_broker_account_blocked_is_hard_block(broker, fake):
    fake.account["trading_blocked"] = True
    pv = preview(broker)
    assert checks(pv)["account_status"]["result"] == "block"


def test_sector_check_uses_known_sector(confirmed):
    c = checks(preview(confirmed))["sector"]
    assert "Technology" in c["name"] and c["result"] == "pass"


# ---------- scheduler ----------

def test_exit_job_runs_before_refresh():
    t, job = scheduler.next_job(datetime(2026, 11, 23, 9, 0, tzinfo=ET))
    assert job == "exits" and (t.hour, t.minute) == (15, 40)
    t, job = scheduler.next_job(datetime(2026, 11, 23, 15, 45, tzinfo=ET))
    assert job == "refresh" and (t.hour, t.minute) == (16, 20)


def test_exit_job_skips_outside_alpaca_mode(client):
    s = scheduler.Scheduler(refresh_fn=lambda t: None, clock=lambda: datetime(2026, 11, 23, 15, 40, tzinfo=ET))
    assert "skipped" in s.run_exits("t")["result"]
