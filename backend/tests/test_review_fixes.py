"""Regression tests for the issues found in the 2026-11-22 code review."""
import json
from datetime import date, datetime

import httpx
import pytest

from app import db
from app.brokers import alpaca as alpaca_mod
from app.live_data import ET
from app.tradingcal import holidays, is_session, next_sessions
from conftest import confirm_with
from test_live_forward import FakeYF, confirm, go_to, live, world  # noqa: F401  (fixtures)


def preview(c, sym="NVDA", **ov):
    r = c.post("/api/previews", json={"symbol": sym, "overrides": ov})
    assert r.status_code == 200, r.text
    return r.json()


def submit(c, pv, **kw):
    return c.post("/api/orders", json={"preview_id": pv["id"], "confirm": True, **kw})


def advance_to(c, day):
    while c.get("/api/status").json()["as_of"] < day:
        assert c.post("/api/sim/advance").status_code == 200


# 1. planned exit must not be lost when the exit day's bar is missing
def test_missing_exit_day_bar_exits_at_next_open(confirmed):
    pv = preview(confirmed)
    submit(confirmed, pv)
    conn = db.connect()
    conn.execute("DELETE FROM bars WHERE symbol='NVDA' AND date=?", (pv["plan"]["exit_date"],))
    conn.close()
    advance_to(confirmed, next_sessions(date.fromisoformat(pv["plan"]["exit_date"]), 1)[0].isoformat())
    j = confirmed.get("/api/journal").json()[0]
    assert j["status"] == "closed" and j["exit_reason"] in ("plan_exit", "stop")
    assert j["exit_date"] > pv["plan"]["exit_date"] or j["exit_reason"] == "stop"
    # and the symbol is tradable again
    assert not any(o["status"] in ("pending", "partially_filled") for o in confirmed.get("/api/orders").json())


# 2. a provider gap (NULL price) must not crash the simulator
def test_null_open_does_not_crash_and_order_keeps_working(confirmed):
    pv = preview(confirmed)
    oid = submit(confirmed, pv).json()["order"]["id"]
    conn = db.connect()
    conn.execute("UPDATE bars SET open=NULL WHERE symbol='NVDA' AND date=?", (pv["plan"]["entry_date"],))
    conn.close()
    r = confirmed.post("/api/sim/advance")
    assert r.status_code == 200 and any("provider gap" in e for e in r.json()["events"])
    assert next(o for o in confirmed.get("/api/orders").json() if o["id"] == oid)["status"] == "pending"
    confirmed.post("/api/sim/advance")
    assert next(o for o in confirmed.get("/api/orders").json() if o["id"] == oid)["status"] == "filled"
    assert confirmed.get("/api/status").status_code == 200


def test_null_close_does_not_break_account(confirmed):
    pv = preview(confirmed)
    submit(confirmed, pv)
    confirmed.post("/api/sim/advance")
    conn = db.connect()
    as_of = db.kv_get(conn, "as_of")
    conn.execute("UPDATE bars SET close=NULL WHERE symbol='NVDA' AND date=?", (as_of,))
    conn.close()
    acct = confirmed.get("/api/status").json()["account"]
    assert acct["positions"] and acct["positions"][0]["last"] > 0


# 4. Alpaca must never act on simulated prices
def test_alpaca_blocked_in_demo_data(client, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    r = client.put("/api/broker/mode", json={"mode": "alpaca_paper"})
    assert r.status_code == 409 and "Live data" in r.json()["detail"]


def test_switching_to_demo_forces_simulator(live, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    assert live.put("/api/broker/mode", json={"mode": "alpaca_paper"}).status_code == 200
    live.put("/api/data/mode", json={"mode": "demo"})
    assert live.get("/api/status").json()["mode"] == "simulator"
    db.set_data_mode("live")


@pytest.fixture
def alpaca_live(live, monkeypatch):
    """Live data + Alpaca mode against the stateful fake Alpaca server."""
    from app import credentials
    from fake_alpaca import GOOD_KEY, GOOD_SECRET, FakeAlpaca
    fake = FakeAlpaca()
    monkeypatch.setattr(alpaca_mod, "TRANSPORT", fake.transport())
    credentials.save_alpaca(GOOD_KEY, GOOD_SECRET)
    assert live.put("/api/broker/mode", json={"mode": "alpaca_paper"}).status_code == 200
    confirm(live)
    return live, fake


# 3. planned exit must not cancel a broker order locally; 4b. GTC stop
def test_alpaca_order_not_canceled_locally_and_uses_gtc(alpaca_live, world):
    c, fake = alpaca_live
    pv = preview(c)
    o = submit(c, pv).json()["order"]
    assert o["broker"] == "alpaca_paper" and o["broker_order_id"] and o["status"] == "pending"
    assert fake.posts[0]["time_in_force"] == "gtc" and fake.posts[0]["order_class"] == "oto"
    go_to(world, "2026-12-02")
    c.post("/api/live/refresh")
    o = next(x for x in c.get("/api/orders").json() if x["id"] == o["id"])
    assert o["status"] == "pending"     # still the broker's to manage, not silently canceled
    fake.fill_entry(pv["id"], 150.0)
    o = next(x for x in c.get("/api/orders").json() if x["id"] == o["id"])
    assert o["status"] == "filled" and o["fills"][0]["price"] == 150.0
    assert c.get("/api/journal").json()[0]["entry_price"] == 150.0


def test_alpaca_broker_error_marks_rejected(alpaca_live, monkeypatch):
    c, _ = alpaca_live
    real = alpaca_mod.AlpacaPaperBroker

    class Flaky(real):
        def submit(self, req):
            raise alpaca_mod.BrokerError("connection reset")

    monkeypatch.setattr(alpaca_mod, "AlpacaPaperBroker", Flaky)
    o = submit(c, preview(c)).json()["order"]
    assert o["status"] == "rejected" and "connection reset" in o["status_reason"]
    assert c.get("/api/journal").json()[0]["status"] == "not_filled"


# 5. rules changed between preview and submit
def test_rule_change_after_preview_is_rejected(confirmed):
    pv = preview(confirmed)
    confirm_with(confirmed, stop_buffer_pct=3.0)
    r = submit(confirmed, pv)
    assert r.status_code == 409 and "Rules changed" in r.json()["detail"]


# 6. close -> cancel -> close again
def test_second_manual_close_after_cancel(confirmed):
    oid = submit(confirmed, preview(confirmed)).json()["order"]["id"]
    confirmed.post("/api/sim/advance")
    detail = confirmed.post(f"/api/orders/{oid}/close").json()
    manual = next(ch for ch in detail["children"] if ch["purpose"] == "manual_exit")
    assert confirmed.post(f"/api/orders/{manual['id']}/cancel").json()["status"] == "canceled"
    assert confirmed.post(f"/api/orders/{oid}/close").status_code == 200


# 7. no fills at an open that printed before the order existed
def test_intraday_submit_waits_for_next_open(live, world):
    confirm(live)
    go_to(world, "2026-11-23", hour=10)          # Monday 10:00 ET, as_of still Friday 11-20
    pv = preview(live)
    assert pv["fills_at_open_of"] == "2026-11-24" and pv["plan"]["entry_date"] == "2026-11-24"
    assert pv["plan"]["shifted_from"] == "2026-11-23"
    oid = submit(live, pv).json()["order"]["id"]
    go_to(world, "2026-11-23", hour=17)
    live.post("/api/live/refresh")
    assert next(o for o in live.get("/api/orders").json() if o["id"] == oid)["status"] == "pending"
    go_to(world, "2026-11-24", hour=17)
    live.post("/api/live/refresh")
    o = next(o for o in live.get("/api/orders").json() if o["id"] == oid)
    assert o["status"] == "filled" and o["fills"][0]["session_date"] == "2026-11-24"


def test_forward_signal_seen_midsession_enters_next_open(live, world):
    confirm(live)
    go_to(world, "2026-11-23", hour=11)
    live.get("/api/watchlist/screen")             # NVDA's 11-20 signal is first seen Monday 11:00
    go_to(world, "2026-11-25", hour=17)
    live.post("/api/live/refresh")
    sig = next(s for s in live.get("/api/forward").json()["signals"] if s["symbol"] == "NVDA")
    assert sig["entry_date"] == "2026-11-24"


# 8. earnings on the signal day (after-close report gaps the entry)
def test_earnings_on_signal_day_flagged():
    from test_indicators_rules import _series
    from app.rules import RuleParams, evaluate_at
    s, nxt, past = _series("NVDA")
    ev = evaluate_at(s, len(s.dates) - 1, RuleParams(), nxt, past + [s.dates[-1]])
    e = next(r for r in ev["rules"] if r["id"] == "earnings")
    assert e["result"] == "warn" and "signal day" in e["observed"]
    assert ev["status"] == "QUALIFIES_WITH_WARNING"


# watchlist: can't drop a symbol you still hold
def test_cannot_remove_symbol_with_open_order(live):
    confirm(live)
    submit(live, preview(live))
    r = live.put("/api/watchlist", json={"symbols": ["SNDK", "COIN", "IREN"]})
    assert r.status_code == 409 and "NVDA" in r.json()["detail"]


# 12. calendar works beyond the old hard-coded years
@pytest.mark.parametrize("d,open_", [
    (date(2028, 4, 14), False),   # Good Friday 2028
    (date(2028, 7, 4), False),
    (date(2027, 12, 31), True),   # New Year's 2028 is a Saturday: no Friday closure
    (date(2030, 11, 28), False),  # Thanksgiving 2030
    (date(2025, 1, 9), False),    # national day of mourning
    (date(2026, 11, 27), True),   # day after Thanksgiving (half day, still a session)
])
def test_calendar_rules(d, open_):
    assert is_session(d) is open_


def test_calendar_matches_published_2026():
    assert holidays(2026) == frozenset({date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
                                        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
                                        date(2026, 11, 26), date(2026, 12, 25)})
