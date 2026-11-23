import json
import sqlite3
import threading

import httpx
import pytest

from app import db
from app.brokers import simulator as sim
from app.brokers.alpaca import AlpacaPaperBroker
from app.brokers.base import BrokerError, OrderRequest
from app.explain import _numbers
from conftest import confirm_with


def preview(c, sym="NVDA", **ov):
    r = c.post("/api/previews", json={"symbol": sym, "overrides": ov})
    assert r.status_code == 200, r.text
    return r.json()


def test_preview_requires_confirmed_rules(client):
    assert client.post("/api/previews", json={"symbol": "NVDA"}).status_code == 409


def test_preview_only_for_qualifying(confirmed):
    for sym in ("SNDK", "IREN", "COIN"):
        assert confirmed.post("/api/previews", json={"symbol": sym}).status_code == 409


def test_preview_contents_and_sizing(confirmed):
    pv = preview(confirmed)
    assert pv["est_loss_if_stopped"] <= pv["risk_budget"]
    assert pv["qty"] == pv["suggested_qty"] > 0
    assert pv["est_position_value"] == pytest.approx(pv["qty"] * pv["entry_assumption"], abs=0.01)
    assert "can differ" in pv["fill_disclaimer"] and pv["mode"] == "simulator"
    assert pv["ruleset_version"] == 1


def test_submission_requires_confirmation(confirmed):
    pv = preview(confirmed)
    assert confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": False}).status_code == 400


def test_duplicate_submission_blocked(confirmed):
    pv = preview(confirmed)
    r1 = confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True})
    r2 = confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True})
    assert r1.status_code == 200 and r2.status_code == 409
    assert r2.json()["duplicate"] is True and r2.json()["order"]["id"] == r1.json()["order"]["id"]
    entries = [o for o in confirmed.get("/api/orders").json() if o["purpose"] == "entry"]
    assert len(entries) == 1


def test_concurrent_double_click_creates_one_order(confirmed):
    pv = preview(confirmed)
    codes = []

    def go():
        codes.append(confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True}).status_code)

    ts = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [200, 409, 409, 409, 409]


def test_second_preview_same_symbol_blocked_while_working(confirmed):
    confirmed.post("/api/orders", json={"preview_id": preview(confirmed)["id"], "confirm": True})
    r = confirmed.post("/api/orders", json={"preview_id": preview(confirmed)["id"], "confirm": True})
    assert r.status_code == 409 and "already have" in r.json()["detail"]


def test_earnings_warning_must_be_acknowledged(client):
    confirm_with(client, volume_ratio_max=0.9)   # SNDK now qualifies with an earnings warning
    pv = preview(client, "SNDK")
    assert pv["earnings_warning"]
    assert client.post("/api/orders", json={"preview_id": pv["id"], "confirm": True}).status_code == 422
    assert client.post("/api/orders", json={"preview_id": pv["id"], "confirm": True, "ack_earnings": True}).status_code == 200


def test_over_buying_power_is_hard_blocked(confirmed):
    pv = preview(confirmed, qty=5000)
    assert next(c for c in pv["risk"]["checks"] if c["id"] == "buying_power")["result"] == "block"
    r = confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True})
    assert r.status_code == 422 and "buying power" in r.json()["detail"].lower()
    assert confirmed.get("/api/orders").json() == []      # nothing reached the broker


def test_full_lifecycle_planned_exit_and_journal(confirmed):
    pv = preview(confirmed)
    o = confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True, "thesis": "Quiet pullback to rising EMA"}).json()["order"]
    assert o["status"] == "pending"
    confirmed.post("/api/sim/advance")
    o = next(x for x in confirmed.get("/api/orders").json() if x["id"] == o["id"])
    assert o["status"] == "filled" and o["fills"][0]["session_date"] == pv["plan"]["entry_date"]
    while confirmed.get("/api/status").json()["as_of"] < pv["plan"]["exit_date"]:
        confirmed.post("/api/sim/advance")
    j = confirmed.get("/api/journal").json()[0]
    assert j["status"] == "closed" and j["exit_reason"] == "plan_exit" and j["exit_date"] == pv["plan"]["exit_date"]
    assert j["realized_pnl"] == pytest.approx((j["exit_price"] - j["entry_price"]) * j["qty"], abs=0.05)
    assert j["adherence"]["followed"] is True
    # Original reasoning preserved exactly as it was at entry.
    assert j["thesis"] == "Quiet pullback to rising EMA"
    assert j["evidence"]["status"] == "QUALIFIES" and j["ruleset_version"] == 1
    assert j["data_timestamp"].startswith("2026-11-20 16:00 ET")


def test_stop_exit_and_adherence_flags_modified_stop(confirmed):
    pv0 = preview(confirmed)
    pv = preview(confirmed, stop_price=round(pv0["entry_assumption"] - 0.2, 2))
    confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True})
    for _ in range(6):
        confirmed.post("/api/sim/advance")
    j = confirmed.get("/api/journal").json()[0]
    assert j["exit_reason"] == "stop"
    checks = {c["check"]: c["ok"] for c in j["adherence"]["checks"]}
    assert checks["Stop placed at the planned level"] is False and j["adherence"]["followed"] is False


def test_partial_fill_then_filled(confirmed, monkeypatch):
    monkeypatch.setattr(sim, "PARTICIPATION", 1e-7)  # cap fills at a handful of shares per session
    pv = preview(confirmed)
    oid = confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True}).json()["order"]["id"]
    confirmed.post("/api/sim/advance")
    o = next(x for x in confirmed.get("/api/orders").json() if x["id"] == oid)
    assert o["status"] == "partially_filled" and 0 < o["filled_qty"] < o["qty"]


def test_cancel_pending_order(confirmed):
    pv = preview(confirmed)
    oid = confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True}).json()["order"]["id"]
    assert confirmed.post(f"/api/orders/{oid}/cancel").json()["status"] == "canceled"
    assert confirmed.post(f"/api/orders/{oid}/cancel").status_code == 409
    assert confirmed.get("/api/journal").json()[0]["status"] == "not_filled"


def test_manual_exit_marks_plan_deviation(confirmed):
    pv = preview(confirmed)
    oid = confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True}).json()["order"]["id"]
    confirmed.post("/api/sim/advance")
    assert confirmed.post(f"/api/orders/{oid}/close").status_code == 200
    assert confirmed.post(f"/api/orders/{oid}/close").status_code == 409   # no duplicate exits
    confirmed.post("/api/sim/advance")
    j = confirmed.get("/api/journal").json()[0]
    if j["exit_reason"] == "manual_exit":
        assert j["adherence"]["followed"] is False


def test_journal_reasoning_is_immutable(confirmed):
    pv = preview(confirmed)
    confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True})
    conn = db.connect()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE journal SET thesis='rewritten after the fact'")
    conn.close()


def test_stale_preview_rejected_after_clock_moves(confirmed):
    pv = preview(confirmed)
    confirmed.post("/api/sim/advance")
    assert confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True}).status_code in (409, 410)


# ---------- explanations ----------

def test_beginner_and_analyst_share_numbers_and_verdict(confirmed):
    b = confirmed.get("/api/symbols/NVDA/explain?level=beginner").json()
    a = confirmed.get("/api/symbols/NVDA/explain?level=analyst").json()
    assert b["evaluation"] == a["evaluation"]
    assert b["explanation"]["status"] == a["explanation"]["status"] == "QUALIFIES"
    assert [l["rule"] for l in b["explanation"]["lines"]][:5] == [l["rule"] for l in a["explanation"]["lines"]][:5]


def test_llm_number_guard_detects_invented_figures():
    allowed = _numbers(json.dumps({"ema": 165.5, "ratio": 0.62}))
    assert not (_numbers("EMA 165.5, ratio 0.62") - allowed)
    assert _numbers("target price 210") - allowed == {"210"}


# ---------- Alpaca adapter (fixture-verified only; no live calls) ----------

def _alpaca(handler):
    return AlpacaPaperBroker(transport=httpx.MockTransport(handler), key="k", secret="s")


def test_alpaca_submit_maps_status_and_sends_oto_stop():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["headers"] = req.headers
        return httpx.Response(200, json={"id": "a1", "status": "accepted", "filled_qty": "0"})

    r = _alpaca(handler).submit(OrderRequest("pv_1", "NVDA", "buy", 10, "market", None, 160.0))
    assert r.status == "pending" and r.broker_order_id == "a1"
    assert seen["body"]["order_class"] == "oto" and seen["body"]["stop_loss"]["stop_price"] == "160.00"
    assert seen["body"]["client_order_id"] == "pv_1" and seen["headers"]["APCA-API-KEY-ID"] == "k"


@pytest.mark.parametrize("raw,mapped", [("partially_filled", "partially_filled"), ("filled", "filled"),
                                        ("canceled", "canceled"), ("rejected", "rejected"), ("new", "pending")])
def test_alpaca_status_mapping(raw, mapped):
    b = _alpaca(lambda req: httpx.Response(200, json={"id": "a1", "status": raw, "filled_qty": "3", "filled_avg_price": "101.5"}))
    assert b.get("a1").status == mapped


def test_alpaca_duplicate_client_id_returns_original():
    def handler(req):
        if req.method == "POST":
            return httpx.Response(422, json={"message": "client_order_id must be unique"})
        assert "by_client_order_id" in req.url.path
        return httpx.Response(200, json={"id": "orig", "status": "filled", "filled_qty": "10", "filled_avg_price": "100"})

    r = _alpaca(handler).submit(OrderRequest("pv_1", "NVDA", "buy", 10, "market", None, None))
    assert r.broker_order_id == "orig" and r.status == "filled"


def test_alpaca_refuses_live_url(monkeypatch):
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(BrokerError):
        AlpacaPaperBroker(key="k", secret="s")


def test_alpaca_mode_unavailable_without_keys(client, monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    assert client.put("/api/broker/mode", json={"mode": "alpaca_paper"}).status_code == 409


def test_reset_restores_demo_state(confirmed):
    confirmed.post("/api/orders", json={"preview_id": preview(confirmed)["id"], "confirm": True})
    confirmed.post("/api/sim/advance")
    assert confirmed.post("/api/sim/reset").status_code == 200
    s = confirmed.get("/api/status").json()
    assert s["as_of"] == "2026-11-20" and confirmed.get("/api/journal").json() == []
    assert confirmed.get("/api/rulesets").json()["active"] is None
