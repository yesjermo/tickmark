"""Phase 2.1/2.2: IBKR through the whole app, and the OAuth connect framework."""
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import credentials, db, oauth
from app.brokers import ibkr
from fake_ibkr import ACCOUNT, FakeIBKR
from test_live_forward import confirm, go_to, live, world  # noqa: F401  (fixtures)


@pytest.fixture
def fib(monkeypatch):
    f = FakeIBKR()
    monkeypatch.setattr(ibkr, "TRANSPORT", f.transport())
    return f


@pytest.fixture
def ib(live, fib):
    st = live.put("/api/broker/ibkr", json={"account_id": ACCOUNT}).json()
    assert st["session"]["authenticated"] and st["accounts"][0]["paper"] is True
    assert live.put("/api/broker/mode", json={"mode": "ibkr"}).status_code == 200
    confirm(live)
    return live


def preview(c, sym="NVDA", **ov):
    r = c.post("/api/previews", json={"symbol": sym, "overrides": ov})
    assert r.status_code == 200, r.text
    return r.json()


def test_ibkr_refuses_non_paper_accounts(live, fib):
    assert live.put("/api/broker/ibkr", json={"account_id": "U7654321"}).status_code == 409
    with pytest.raises(Exception):
        ibkr.IBKRBroker(account_id="U7654321")


def test_ibkr_mode_needs_connection(live, fib):
    assert live.put("/api/broker/mode", json={"mode": "ibkr"}).status_code == 409


def test_ibkr_status_reports_gateway_problems(live, fib):
    fib.authenticated = False
    st = live.get("/api/broker/ibkr").json()
    assert st["session"]["authenticated"] is False and st["accounts"] == []


def test_ibkr_full_lifecycle(ib, fib, world):
    pv = preview(ib)
    assert pv["equity_source"] == "Interactive Brokers account" and pv["equity"] == 100000.0   # sized from the IBKR ledger
    r = ib.post("/api/orders", json={"preview_id": pv["id"], "confirm": True})
    assert r.status_code == 200, r.text
    o = r.json()["order"]
    assert o["broker"] == "ibkr" and o["broker_order_id"]
    sent = fib.posts[0]["orders"]
    assert sent[0]["cOID"] == pv["id"] and sent[1]["parentId"] == pv["id"] and sent[1]["orderType"] == "STP"
    fib.fill_entry(pv["id"], 168.0)
    synced = next(x for x in ib.get("/api/orders").json() if x["id"] == o["id"])
    assert synced["status"] == "filled" and synced["fills"][0]["price"] == 168.0
    assert any(ch["purpose"] == "stop" and ch["status"] == "pending" for ch in synced["children"])
    assert ib.post("/api/reconcile").json()["ok"] is True
    go_to(world, pv["plan"]["exit_date"], hour=15)
    world["clock"] = world["clock"].replace(minute=40)
    ev = ib.post("/api/broker/exits").json()["events"]
    assert any("planned exit sent" in e for e in ev)
    stop = next(x for x in fib.orders if x["orderType"] == "STP")
    assert stop["status"] == "Cancelled"
    exit_ticket = fib.posts[-1]["orders"][0]
    assert exit_ticket["orderType"] == "MKT" and exit_ticket["side"] == "SELL"   # no MOC on this API: market near the close
    fib.fill_sells("NVDA", 175.0)
    ib.get("/api/orders")
    j = ib.get("/api/journal").json()[0]
    assert j["status"] == "closed" and j["exit_reason"] == "plan_exit"
    assert j["realized_pnl"] == pytest.approx((175.0 - 168.0) * pv["qty"], abs=0.01)


def test_ibkr_not_allowed_for_real_money(live, fib, monkeypatch):
    live.put("/api/broker/ibkr", json={"account_id": ACCOUNT})
    db.set_setting("environment", "real")
    db.set_setting("live_enabled_at", "2026-11-22T00:00:00+00:00")
    try:
        assert live.put("/api/broker/mode", json={"mode": "ibkr"}).status_code == 409
    finally:
        db.set_setting("environment", "paper")
        db.set_setting("live_enabled_at", None)


def test_catalogue_shows_ibkr_available(client):
    cat = {b["id"]: b for b in client.get("/api/brokers").json()}
    assert cat["ibkr"]["status"] == "available"


# ---------- OAuth framework ----------

class FakeTokenServer:
    def __init__(self):
        self.requests = []
        self.n = 0

    def handle(self, req):
        form = parse_qs(req.content.decode())
        self.requests.append({"url": str(req.url), "form": form, "auth": req.headers.get("Authorization")})
        self.n += 1
        return httpx.Response(200, json={"access_token": f"at{self.n}", "refresh_token": f"rt{self.n}", "expires_in": 1800})


@pytest.fixture
def tok(monkeypatch):
    t = FakeTokenServer()
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(t.handle))
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "cid123")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("TRADESTATION_CLIENT_ID", "tsid")
    return t


def test_oauth_needs_registered_app(client, monkeypatch):
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    r = client.get("/api/oauth/schwab/start")
    assert r.status_code == 409 and "SCHWAB_CLIENT_ID" in r.json()["detail"]


def test_oauth_full_flow_and_refresh(client, tok):
    start = client.get("/api/oauth/schwab/start").json()
    q = parse_qs(urlparse(start["authorize_url"]).query)
    assert q["client_id"] == ["cid123"] and q["state"] == [start["state"]] and "/api/oauth/schwab/callback" in q["redirect_uri"][0]
    assert client.get("/api/oauth/schwab/callback", params={"code": "c1", "state": "forged"}).status_code == 400
    r = client.get("/api/oauth/schwab/callback", params={"code": "c1", "state": start["state"]})
    assert r.status_code == 200 and "Connected to Charles Schwab" in r.text
    assert tok.requests[-1]["form"]["grant_type"] == ["authorization_code"] and tok.requests[-1]["auth"].startswith("Basic ")
    st = next(s for s in client.get("/api/oauth").json() if s["id"] == "schwab")
    assert st["connected"] and 6.9 <= st["relogin_in_days"] <= 7.0
    assert "at1" not in str(client.get("/api/oauth").json())          # tokens never leave the server
    assert client.get("/api/oauth/schwab/callback", params={"code": "c1", "state": start["state"]}).status_code == 400   # state is single-use
    # Force expiry: the next access triggers a refresh-token grant.
    t = oauth.tokens("schwab")
    t["expires_at"] = int(time.time()) - 5
    credentials.set_secret("oauth_schwab_tokens", __import__("json").dumps(t))
    assert oauth.access_token("schwab") == "at2"
    assert tok.requests[-1]["form"]["grant_type"] == ["refresh_token"] and tok.requests[-1]["form"]["refresh_token"] == ["rt1"]


def test_oauth_pkce_for_tradestation(client, tok):
    start = client.get("/api/oauth/tradestation/start").json()
    q = parse_qs(urlparse(start["authorize_url"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"][0]
    client.get("/api/oauth/tradestation/callback", params={"code": "c9", "state": start["state"]})
    assert tok.requests[-1]["form"]["code_verifier"][0]
    assert tok.requests[-1]["form"]["client_id"] == ["tsid"]


def test_relogin_alert_before_refresh_token_expires(client, tok):
    start = client.get("/api/oauth/schwab/start").json()
    client.get("/api/oauth/schwab/callback", params={"code": "c1", "state": start["state"]})
    t = oauth.tokens("schwab")
    t["refresh_expires_at"] = int(time.time()) + 3600
    credentials.set_secret("oauth_schwab_tokens", __import__("json").dumps(t))
    conn = db.connect()
    assert oauth.expiry_alerts(conn) == 1
    conn.close()
    a = client.get("/api/alerts").json()["alerts"][0]
    assert "log in again" in a["title"] and a["severity"] == "warn"


def test_oauth_disconnect(client, tok):
    start = client.get("/api/oauth/schwab/start").json()
    client.get("/api/oauth/schwab/callback", params={"code": "c1", "state": start["state"]})
    assert client.delete("/api/oauth/schwab").json()["connected"] is False


def test_unreachable_gateway_gives_mac_port_hint(live, monkeypatch):
    def boom(req):
        raise httpx.ConnectTimeout("handshake timed out")
    monkeypatch.setattr(ibkr, "TRANSPORT", httpx.MockTransport(boom))
    monkeypatch.setattr(ibkr.sys, "platform", "darwin")
    st = live.get("/api/broker/ibkr").json()
    assert "AirPlay Receiver" in st["error"] and "listenPort" in st["error"]
