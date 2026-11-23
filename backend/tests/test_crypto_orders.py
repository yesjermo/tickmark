"""Crypto order routing: test/demo accounts only, preview checks, duplicate protection, exchange-side stop after fill."""
import math

import ccxt
import pytest

from app import copilot, db, linked
from test_live_forward import live, world  # noqa: F401  (fixtures)


class FakeOKX:
    """Stateful stand-in for a ccxt exchange (spot)."""
    def __init__(self, conf, book):
        self.conf, self.book, self.sandbox = conf, book, False

    def set_sandbox_mode(self, on):
        self.sandbox = on

    def load_markets(self):
        return {"BTC/USDT": {"spot": True, "active": True, "base": "BTC", "quote": "USDT",
                             "limits": {"amount": {"min": 0.0001}, "cost": {"min": 5}}},
                "ETH/USDT": {"spot": True, "active": True, "base": "ETH", "quote": "USDT", "limits": {}}}

    def fetch_ticker(self, s):
        return {"bid": 59990.0, "ask": 60010.0, "last": 60000.0} if s == "BTC/USDT" else {"bid": 2999.0, "ask": 3001.0, "last": 3000.0}

    def fetch_balance(self):
        return {"total": dict(self.book["bal"]), "free": dict(self.book["bal"])}

    def amount_to_precision(self, s, a):
        return str(math.floor(a * 1e6) / 1e6)

    def price_to_precision(self, s, p):
        return str(round(float(p), 1))

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        cid = (params or {}).get("clientOrderId")
        if any(o["clientOrderId"] == cid for o in self.book["orders"].values()):
            raise ccxt.InvalidOrder("duplicate clOrdId")
        oid = f"x{len(self.book['orders']) + 1}"
        o = {"id": oid, "clientOrderId": cid, "symbol": symbol, "type": type_, "side": side, "amount": amount, "price": price,
             "status": "open", "filled": 0.0, "average": None, "cost": 0.0, "trigger": False}
        self.book["orders"][oid] = o
        self.book["calls"].append(("create_order", symbol, type_, side, amount))
        return dict(o)

    def create_stop_order(self, symbol, type_, side, amount, price=None, trigger=None, params=None):
        if self.book.get("stop_fail"):
            raise ccxt.InvalidOrder("trigger orders not allowed")
        o = self.create_order(symbol, type_, side, amount, price, params)
        self.book["orders"][o["id"]].update(trigger=True, stopPrice=trigger)
        self.book["calls"].append(("create_stop_order", symbol, trigger, amount))
        return dict(self.book["orders"][o["id"]])

    def fetch_order(self, oid, symbol=None, params=None):
        return dict(self.book["orders"][oid])

    def cancel_order(self, oid, symbol=None, params=None):
        self.book["orders"][oid]["status"] = "canceled"
        self.book["calls"].append(("cancel_order", oid))
        return dict(self.book["orders"][oid])

    # helpers
    def fill(self, oid, price):
        o = self.book["orders"][oid]
        o.update(status="closed", filled=o["amount"], average=price, cost=o["amount"] * price)


@pytest.fixture
def okx(monkeypatch):
    book = {"bal": {"USDT": 10000.0, "BTC": 0.0}, "orders": {}, "calls": []}
    holder = {}

    def factory(eid, conf):
        holder["ex"] = FakeOKX(conf, book)
        return holder["ex"]
    monkeypatch.setattr(linked, "CCXT_FACTORY", factory)
    book["fill"] = lambda oid, px: FakeOKX(None, book).fill(oid, px)
    return book


def link(c, provider="okx", sandbox=True):
    r = c.post("/api/linked", json={"provider": provider, "sandbox": sandbox,
                                    "values": {"apiKey": "k", "secret": "s", "password": "p"}})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def preview(c, aid, **kw):
    body = {"account_id": aid, "symbol": "BTC/USDT", "side": "buy", "notional": 1000, **kw}
    r = c.post("/api/crypto/preview", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_preview_sizes_with_precision_and_checks(live, okx):
    aid = link(live)
    pv = preview(live, aid, stop_price=57000)
    assert pv["amount"] == pytest.approx(math.floor(1000 / 60010 * 1e6) / 1e6) and pv["price"] == 60010.0   # buys use the ask
    assert pv["environment"] == "testnet / demo" and not pv["blocked"]
    assert pv["est_loss_if_stopped"] == pytest.approx((60010 - 57000) * pv["amount"], abs=0.01)
    assert {x["id"] for x in pv["checks"]} >= {"kill_switch", "restricted", "funds", "min_amount", "min_cost", "stop", "size"}


def test_hard_blocks(live, okx):
    aid = link(live)
    assert preview(live, aid, notional=50000)["blocked"]                        # more than free USDT
    assert preview(live, aid, notional=1)["blocked"]                            # under the $5 minimum
    assert preview(live, aid, stop_price=61000)["blocked"]                      # stop above entry
    live.put("/api/risk/limits", json={"params": {"restricted": ["BTC"]}})
    pv = preview(live, aid)
    assert pv["blocked"] and next(x for x in pv["checks"] if x["id"] == "restricted")["result"] == "block"
    r = live.post("/api/crypto/orders", json={"preview_id": pv["id"], "confirm": True})
    assert r.status_code == 409 and okx["calls"] == []


def test_real_account_needs_its_live_switch_and_typed_confirm(live, okx):
    aid = link(live, "kraken", sandbox=True)                                    # Kraken has no sandbox: linked as real
    acc = next(a for a in live.get("/api/crypto/accounts").json() if a["id"] == aid)
    assert acc["can_trade"] is False and "read-only until" in acc["why"]
    r = live.post("/api/crypto/preview", json={"account_id": aid, "symbol": "BTC/USDT", "side": "buy", "notional": 100})
    assert r.status_code == 409 and "real-money trading isn't enabled" in r.json()["detail"]
    r = live.put(f"/api/linked/{aid}/live", json={"on": True, "phrase": linked.LIVE_PHRASE})
    assert r.status_code == 409 and "Risk limits" in r.json()["detail"]          # limits must be saved deliberately
    live.put("/api/risk/limits", json={"params": {}, "note": "deliberate"})
    assert live.put(f"/api/linked/{aid}/live", json={"on": True, "phrase": "ok"}).status_code == 422
    assert live.put(f"/api/linked/{aid}/live", json={"on": True, "phrase": linked.LIVE_PHRASE}).json()["live_enabled_at"]
    acc = next(a for a in live.get("/api/crypto/accounts").json() if a["id"] == aid)
    assert acc["can_trade"] and acc["environment"] == "LIVE (real money)"
    pv = preview(live, aid, notional=1000)
    assert pv["live"] and pv["environment"] == "LIVE (real money)" and any(x["id"] == "daily_loss" for x in pv["checks"])
    r = live.post("/api/crypto/orders", json={"preview_id": pv["id"], "confirm": True})
    assert r.status_code == 422 and "type BTC" in r.json()["detail"] and okx["calls"] == []
    o = live.post("/api/crypto/orders", json={"preview_id": pv["id"], "confirm": True, "live_confirm": "btc"}).json()["order"]
    assert o["environment"] == "LIVE (real money)" and okx["calls"][0][0] == "create_order"
    live.put(f"/api/linked/{aid}/live", json={"on": False})
    assert live.post("/api/crypto/preview", json={"account_id": aid, "symbol": "BTC/USDT", "side": "buy", "notional": 100}).status_code == 409


def test_testnet_accounts_cannot_get_a_live_switch(live, okx):
    aid = link(live)
    live.put("/api/risk/limits", json={"params": {}, "note": "deliberate"})
    r = live.put(f"/api/linked/{aid}/live", json={"on": True, "phrase": linked.LIVE_PHRASE})
    assert r.status_code == 409 and "Linked as a real account" in r.json()["detail"]


def test_submit_fill_then_exchange_stop_then_close(live, okx):
    aid = link(live)
    pv = preview(live, aid, stop_price=57000)
    assert live.post("/api/crypto/orders", json={"preview_id": pv["id"], "confirm": False}).status_code == 400
    o = live.post("/api/crypto/orders", json={"preview_id": pv["id"], "confirm": True}).json()["order"]
    assert o["status"] == "open" and o["client_order_id"] == pv["id"]
    assert okx["orders"]["x1"]["clientOrderId"] == pv["id"] and okx["orders"]["x1"]["type"] == "market"
    dup = live.post("/api/crypto/orders", json={"preview_id": pv["id"], "confirm": True})
    assert dup.status_code == 409 and len(okx["orders"]) == 1
    okx["fill"]("x1", 60020.0)
    orders = live.get("/api/crypto/orders").json()                             # sync: fill, then the stop
    (e,) = orders
    assert e["status"] == "filled" and e["average"] == 60020.0
    (stop,) = e["children"]
    assert stop["purpose"] == "stop" and stop["stop_price"] == 57000 and stop["status"] == "open"
    assert ("create_stop_order", "BTC/USDT", 57000.0, pv["amount"]) in okx["calls"]
    live.get("/api/crypto/orders")
    assert sum(1 for c in okx["calls"] if c[0] == "create_stop_order") == 1   # placed once only
    closed = live.post(f"/api/crypto/orders/{e['id']}/close").json()
    kids = {k["purpose"]: k for k in closed["children"]}
    assert kids["stop"]["status"] == "canceled" and kids["exit"]["side"] == "sell" and kids["exit"]["order_type"] == "market"
    assert okx["calls"].index(("cancel_order", stop["exchange_order_id"])) < len(okx["calls"]) - 1   # stop canceled before the sell


def test_stop_failure_raises_critical_alert(live, okx):
    aid = link(live)
    okx["stop_fail"] = True
    pv = preview(live, aid, stop_price=57000)
    live.post("/api/crypto/orders", json={"preview_id": pv["id"], "confirm": True})
    okx["fill"]("x1", 60000.0)
    (e,) = live.get("/api/crypto/orders").json()
    assert e["children"][0]["status"] == "rejected"
    al = live.get("/api/alerts").json()["alerts"]
    assert any(a["severity"] == "critical" and "WITHOUT a stop" in a["title"] for a in al)


def test_cancel_and_kill_switch(live, okx):
    aid = link(live)
    pv = preview(live, aid)
    o = live.post("/api/crypto/orders", json={"preview_id": pv["id"], "confirm": True}).json()["order"]
    assert live.post(f"/api/crypto/orders/{o['id']}/cancel").json()["status"] == "canceled"
    pv2 = preview(live, aid)
    live.put("/api/risk/kill-switch", json={"on": True, "reason": "testing the switch"})
    r = live.post("/api/crypto/orders", json={"preview_id": pv2["id"], "confirm": True})
    assert r.status_code == 409 and "kill switch" in r.json()["detail"]


def test_needs_live_data(client, okx):
    aid = link(client)
    r = client.post("/api/crypto/preview", json={"account_id": aid, "symbol": "BTC/USDT", "side": "buy", "notional": 100})
    assert r.status_code == 409 and "Live data" in r.json()["detail"]


def test_copilot_crypto_proposal_opens_the_crypto_tab(live, okx, monkeypatch):
    from test_copilot import FakeClaude, msg
    aid = link(live)
    fake = FakeClaude([msg([("propose_crypto_order", {"account_id": aid, "symbol": "BTC/USDT", "side": "buy", "notional": 500,
                                                     "stop_price": 57000, "rationale": "Test the routing."})], "tool_use"), msg(["Drafted."])])
    monkeypatch.setattr(copilot, "CLIENT_FACTORY", lambda: fake)
    (p,) = live.post("/api/copilot/chat", json={"text": "buy some BTC", "model": "claude-opus-5-5"}).json()["proposals"]
    assert p["kind"] == "crypto_order" and p["payload"]["environment"] == "testnet / demo" and okx["calls"] == []
    r = live.post(f"/api/copilot/proposals/{p['id']}/apply").json()
    assert r["open"] == "crypto" and r["form"]["notional"] == 500 and okx["calls"] == []    # nothing sent by the copilot


def test_crypto_journal_and_pnl_stats(live, okx):
    aid = link(live)
    pv = preview(live, aid, stop_price=57000)
    o = live.post("/api/crypto/orders", json={"preview_id": pv["id"], "confirm": True, "thesis": "Breakout retest."}).json()["order"]
    okx["fill"]("x1", 60000.0)
    okx["orders"]["x1"]["fee"] = {"cost": 0.06, "currency": "USDT"}
    live.get("/api/crypto/orders")                                   # sync fill + stop
    (j,) = live.get("/api/crypto/journal").json()
    assert j["status"] == "open" and j["realized_pnl"] is None and j["adherence"]["followed"] is True
    live.post(f"/api/crypto/orders/{o['id']}/close")
    exit_id = max(okx["orders"])                                     # the close order
    okx["fill"](exit_id, 63000.0)
    okx["orders"][exit_id]["fee"] = {"cost": 0.07, "currency": "USDT"}
    live.get("/api/crypto/orders")
    (j,) = live.get("/api/crypto/journal").json()
    amt = pv["amount"]
    assert j["status"] == "closed" and j["exit_reason"] == "manual_exit" and j["thesis"] == "Breakout retest."
    assert j["realized_pnl"] == pytest.approx(round((63000 - 60000) * amt - 0.13, 2))
    st = live.get("/api/pnl").json()
    assert st["crypto"]["closed"] == 1 and st["crypto"]["win_rate_pct"] == 100.0
    assert st["all"]["realized_pnl"] == pytest.approx(j["realized_pnl"]) and "OKX" in st["by_venue"]


def test_stopped_out_crypto_is_a_loss(live, okx):
    aid = link(live)
    pv = preview(live, aid, stop_price=57000)
    live.post("/api/crypto/orders", json={"preview_id": pv["id"], "confirm": True})
    okx["fill"]("x1", 60000.0)
    live.get("/api/crypto/orders")
    okx["fill"]("x2", 56900.0)                                       # the stop
    live.get("/api/crypto/orders")
    (j,) = live.get("/api/crypto/journal").json()
    assert j["exit_reason"] == "stop" and j["realized_pnl"] < 0
    assert live.get("/api/pnl").json()["crypto"]["profit_factor"] == 0.0
