"""Day trades routed to Alpaca Paper as brackets: routing rules, bracket legs, target/stop/flat exits, the day monitor."""
from datetime import datetime

import pytest

from app import broker_sync, day_monitor, db, intraday
from app.brokers import alpaca as alpaca_mod
from app.brokers.base import OrderRequest
from app.live_data import ET
from fake_alpaca import GOOD_KEY, GOOD_SECRET, FakeAlpaca
from test_daytrading import DAY, scenario, start_replay, step_to  # noqa: F401  (fixtures)
from test_live_forward import live, world  # noqa: F401  (fixtures)


@pytest.fixture
def fake(monkeypatch):
    f = FakeAlpaca()
    monkeypatch.setattr(alpaca_mod, "TRANSPORT", f.transport())
    monkeypatch.setattr(intraday, "latest_quote",
                        lambda sym: {"bid": 99.39, "ask": 99.41, "spread_bps": 2.0, "age_s": 1})
    return f


@pytest.fixture
def broker(live, fake):
    r = live.post("/api/broker/connect", json={"key_id": GOOD_KEY, "secret": GOOD_SECRET})
    assert r.status_code == 200, r.text
    assert live.put("/api/broker/mode", json={"mode": "alpaca_paper"}).status_code == 200
    assert live.put("/api/day/rules", json={"params": {"strategy": "vwap_reclaim"}}).status_code == 200
    return live


def at(world, hhmm, day=DAY):
    h, m = map(int, hhmm.split(":"))
    world["clock"] = datetime(*map(int, day.split("-")), h, m, tzinfo=ET)


def live_trade(c, world):
    """Live session at 10:31 ET on the scenario day: NVDA reclaims VWAP on the 10:25 bar."""
    at(world, "10:31")
    nv = next(r for r in c.get("/api/day/screen").json()["results"] if r["symbol"] == "NVDA")
    assert nv["status"] == "QUALIFIES", nv
    pv = c.post("/api/day/preview", json={"symbol": "NVDA"}).json()
    assert pv["route"]["broker"] == "alpaca_paper", pv["route"]
    r = c.post("/api/day/orders", json={"preview_id": pv["id"], "confirm": True})
    assert r.status_code == 200, r.text
    return pv, r.json()["order"]


def rows(where, *args):
    c = db.connect()
    try:
        return [dict(r) for r in c.execute(f"SELECT * FROM orders WHERE {where}", args)]
    finally:
        c.close()


def test_bracket_body_shape(fake, monkeypatch):
    monkeypatch.setattr(alpaca_mod.credentials, "get_alpaca", lambda env=None: (GOOD_KEY, GOOD_SECRET, "paper"))
    b = alpaca_mod.AlpacaPaperBroker()
    res = b.submit_bracket(OrderRequest("dv_x", "NVDA", "buy", 10, "market", None, 98.5), 101.25)
    body = fake.posts[-1]
    assert body["order_class"] == "bracket" and body["time_in_force"] == "day" and body["client_order_id"] == "dv_x"
    assert body["take_profit"] == {"limit_price": "101.25"} and body["stop_loss"] == {"stop_price": "98.50"}
    assert {l["type"] for l in res.legs} == {"limit", "stop"} and all(l["side"] == "sell" for l in res.legs)


def test_replay_with_alpaca_connected_stays_simulated(broker, fake, scenario):
    start_replay(broker)
    step_to(broker, "10:30")
    pv = broker.post("/api/day/preview", json={"symbol": "NVDA"}).json()
    assert pv["route"]["broker"] == "simulator" and "Replays are always simulated" in pv["route"]["why"]
    r = broker.post("/api/day/orders", json={"preview_id": pv["id"], "confirm": True})
    assert r.status_code == 200 and r.json()["order"]["broker"] == "simulator"
    assert not any(p.get("order_class") == "bracket" for p in fake.posts)


def test_live_day_trade_goes_to_alpaca_and_target_closes_it(broker, fake, world, scenario):
    pv, o = live_trade(broker, world)
    assert o["broker"] == "alpaca_paper"
    body = fake.posts[-1]
    assert body["order_class"] == "bracket" and body["client_order_id"] == pv["id"]
    assert body["take_profit"]["limit_price"] == f"{pv['target']:.2f}"
    legs = rows("parent_id=?", o["id"])
    assert {l["purpose"] for l in legs} == {"stop", "target"} and all(l["style"] == "intraday" for l in legs)
    fake.fill_entry(pv["id"], 99.42)
    fake.fill_target("NVDA", pv["target"])
    broker_sync.sync()
    j = next(x for x in broker.get("/api/journal").json() if x["symbol"] == "NVDA")
    assert j["status"] == "closed" and j["exit_reason"] == "target" and j["realized_pnl"] > 0
    assert j["adherence"]["followed"] is True
    stop = rows("parent_id=? AND purpose='stop'", o["id"])[0]
    assert stop["status"] == "canceled"                     # OCO sibling


def test_stop_leg_closes_with_reason_stop(broker, fake, world, scenario):
    pv, o = live_trade(broker, world)
    fake.fill_entry(pv["id"], 99.42)
    fake.trigger_stop("NVDA", pv["stop_price"] - 0.05)
    broker_sync.sync()
    j = next(x for x in broker.get("/api/journal").json() if x["symbol"] == "NVDA")
    assert j["exit_reason"] == "stop" and j["realized_pnl"] < 0


def test_monitor_flattens_at_flat_by(broker, fake, world, scenario):
    pv, o = live_trade(broker, world)
    fake.fill_entry(pv["id"], 99.42)
    at(world, "15:30")
    day_monitor.tick(world["clock"])
    assert not rows("parent_id=? AND purpose='flat'", o["id"])   # not yet
    at(world, "15:55")
    ev = day_monitor.tick(world["clock"])
    assert any("flattened" in e for e in ev), ev
    sell = fake.posts[-1]
    assert sell["side"] == "sell" and sell["type"] == "market" and sell["client_order_id"].endswith(":flat:1")
    legs = {l["purpose"]: l for l in rows("parent_id=?", o["id"])}
    assert legs["stop"]["status"] == "canceled" and legs["target"]["status"] == "canceled"
    fake.fill_sells("NVDA", 99.6)
    broker_sync.sync()
    j = next(x for x in broker.get("/api/journal").json() if x["symbol"] == "NVDA")
    assert j["exit_reason"] == "flat" and j["adherence"]["followed"] is True
    day_monitor.tick(world["clock"])                           # idempotent: no second sell
    assert sum(1 for p in fake.posts if p["side"] == "sell") == 1


def test_monitor_cancels_unfilled_entry_and_alerts_after_close(broker, fake, world, scenario):
    pv, o = live_trade(broker, world)
    at(world, "15:56")
    ev = day_monitor.tick(world["clock"])
    assert any("canceled" in e for e in ev)
    assert rows("id=?", o["id"])[0]["status"] == "canceled"
    assert fake.by_client(pv["id"])["status"] == "canceled"


def test_open_after_close_raises_critical_alert(broker, fake, world, scenario):
    pv, o = live_trade(broker, world)
    fake.fill_entry(pv["id"], 99.42)
    broker_sync.sync()
    at(world, "16:02")
    ev = day_monitor.tick(world["clock"])
    assert any("still open after close" in e for e in ev)
    feed = broker.get("/api/alerts").json()
    assert any(a["kind"] == "day" and a["severity"] == "critical" for a in feed["alerts"])


def test_manual_flatten_goes_through_broker(broker, fake, world, scenario):
    pv, o = live_trade(broker, world)
    fake.fill_entry(pv["id"], 99.42)
    at(world, "11:00")
    r = broker.post(f"/api/day/orders/{o['id']}/flatten")
    assert r.status_code == 200, r.text
    assert fake.posts[-1]["side"] == "sell" and ":manual:" in fake.posts[-1]["client_order_id"]
    # the local intraday simulator must not fill a broker exit
    at(world, "11:20")
    broker.get("/api/day/state")
    from app import day
    day.sync_live()
    ex = rows("parent_id=? AND purpose='manual_exit'", o["id"])[0]
    assert ex["broker"] == "alpaca_paper" and ex["filled_qty"] == 0


def test_planned_exits_ignore_day_trades(broker, fake, world, scenario):
    pv, o = live_trade(broker, world)
    fake.fill_entry(pv["id"], 99.42)
    broker_sync.sync()
    at(world, "15:41")
    assert broker_sync.run_planned_exits(world["clock"]) == []


def test_real_money_environment_blocks_broker_day_trades(broker, fake, world, scenario, monkeypatch):
    at(world, "10:31")
    c = db.connect()                                   # paper db open; then flip the environment flag
    monkeypatch.setattr(db, "environment", lambda: "real")
    try:
        from app import day
        rt = day.route(c, "live")
    finally:
        c.close()
    assert rt["blocked"] and "real money" in rt["why"]
