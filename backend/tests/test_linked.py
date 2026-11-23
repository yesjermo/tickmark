"""Linked accounts (read-only): crypto exchanges via ccxt, Robinhood Crypto, moomoo/Futu OpenD, Phantom watch-only."""
import base64
import json
import types

import ccxt
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app import copilot, credentials, linked


class FakeExchange:
    created: list = []

    def __init__(self, eid, conf, balance=None, fail=None):
        self.eid, self.conf, self.sandbox, self.fail = eid, conf, False, fail
        self.balance = balance or {"total": {"BTC": 0.5, "USDT": 1000.0, "DOGE": 0.0, "XYZ": 3.0},
                                   "free": {"BTC": 0.4, "USDT": 1000.0, "XYZ": 3.0}}
        self.calls = []
        FakeExchange.created.append(self)

    def set_sandbox_mode(self, on):
        self.sandbox = on

    def fetch_balance(self):
        self.calls.append("fetch_balance")
        if self.fail:
            raise self.fail
        return self.balance

    def load_markets(self):
        return {"BTC/USDT": {}, "BTC/USD": {}}

    def fetch_ticker(self, sym):
        return {"last": 60000.0}

    # anything that could trade must never be called
    def create_order(self, *a, **k):
        raise AssertionError("linked accounts must never place orders")


@pytest.fixture
def ex(monkeypatch):
    FakeExchange.created = []
    state = {}
    monkeypatch.setattr(linked, "CCXT_FACTORY", lambda eid, conf: FakeExchange(eid, conf, **state))
    return state


def test_catalogue_lists_new_brokers_and_exchanges(client):
    cat = {b["id"]: b for b in client.get("/api/brokers").json()}
    for b in ("robinhood", "moomoo", "webull", "fidelity", "coinbase", "binance", "kraken", "okx", "bybit", "phantom"):
        assert b in cat
    assert cat["fidelity"]["status"] == "planned" and "no public trading API" in cat["fidelity"]["how"]
    assert cat["phantom"]["connect_label"] == "Public address" and "seed phrase" in cat["phantom"]["how"]
    provs = {p["id"] for p in client.get("/api/linked").json()["providers"]}
    assert {cat[b]["linked_provider"] for b in cat if cat[b].get("linked_provider")} <= provs


def test_binance_testnet_link_values_holdings_and_keeps_secret_in_keychain(client, ex):
    r = client.post("/api/linked", json={"provider": "binance", "label": "Binance testnet", "sandbox": True,
                                         "values": {"apiKey": "k-123", "secret": "s-secret-456"}})
    assert r.status_code == 200, r.text
    a = r.json()
    fx = FakeExchange.created[-1]
    assert fx.eid == "binance" and fx.sandbox is True and fx.conf["apiKey"] == "k-123"
    h = {x["asset"]: x for x in a["snapshot"]["holdings"]}
    assert "DOGE" not in h and h["BTC"]["usd_value"] == 30000.0 and h["USDT"]["usd_value"] == 1000.0
    assert h["XYZ"]["usd_value"] is None and a["snapshot"]["total_usd"] == 31000.0
    assert a["snapshot"]["environment"] == "testnet / paper"
    body = json.dumps(client.get("/api/linked").json())
    assert "s-secret-456" not in body and "k-123" in body                  # key id visible, secret never
    assert credentials.get_secret(f"linked:{a['id']}:secret") == "s-secret-456"
    assert client.post(f"/api/linked/{a['id']}/refresh").json()["snapshot"]["total_usd"] == 31000.0
    client.delete(f"/api/linked/{a['id']}")
    assert credentials.get_secret(f"linked:{a['id']}:secret") is None and client.get("/api/linked").json()["accounts"] == []


def test_coinbase_and_kraken_never_claim_a_sandbox(client, ex):
    a = client.post("/api/linked", json={"provider": "kraken", "sandbox": True, "values": {"apiKey": "k", "secret": "s"}}).json()
    assert a["sandbox"] is False and FakeExchange.created[-1].sandbox is False
    assert a["snapshot"]["environment"] == "real account (read-only)"


def test_okx_needs_passphrase_and_bad_key_is_not_saved(client, ex):
    r = client.post("/api/linked", json={"provider": "okx", "values": {"apiKey": "k", "secret": "s"}})
    assert r.status_code == 400 and "Passphrase" in r.json()["detail"]
    ex["fail"] = ccxt.AuthenticationError("invalid api key")
    r = client.post("/api/linked", json={"provider": "okx", "values": {"apiKey": "k", "secret": "s", "password": "p"}})
    assert r.status_code == 400 and "rejected the key" in r.json()["detail"]
    assert client.get("/api/linked").json()["accounts"] == []


def test_robinhood_crypto_signs_requests(client, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization
    seed = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub: Ed25519PublicKey = priv.public_key()
    seen = []

    def handle(req: httpx.Request):
        path = req.url.raw_path.decode()
        msg = f"{req.headers['x-api-key']}{req.headers['x-timestamp']}{path}GET".encode()
        pub.verify(base64.b64decode(req.headers["x-signature"]), msg)      # raises if the signature is wrong
        seen.append(path)
        if path.startswith("/api/v1/crypto/trading/accounts/"):
            return httpx.Response(200, json={"account_number": "123456789", "status": "active", "buying_power": "250.50", "buying_power_currency": "USD"})
        if path.startswith("/api/v1/crypto/trading/holdings/"):
            return httpx.Response(200, json={"results": [{"asset_code": "BTC", "total_quantity": "0.01", "quantity_available_for_trading": "0.01"}], "next": None})
        if path.startswith("/api/v1/crypto/marketdata/best_bid_ask/"):
            return httpx.Response(200, json={"results": [{"symbol": "BTC-USD", "price": "60000"}]})
        return httpx.Response(404)
    monkeypatch.setattr(linked, "ROBINHOOD_TRANSPORT", httpx.MockTransport(handle))
    a = client.post("/api/linked", json={"provider": "robinhood_crypto", "values": {"api_key": "rh-api-x", "private_key": base64.b64encode(seed).decode()}})
    assert a.status_code == 200, a.text
    snap = a.json()["snapshot"]
    assert snap["total_usd"] == 850.5 and snap["account"]["number_hint"] == "6789"
    assert all(not p.startswith("/api/v1/crypto/trading/orders") for p in seen)


def test_phantom_watch_only(client, monkeypatch):
    assert client.post("/api/linked", json={"provider": "phantom", "values": {"address": "abandon ability able about above absent absorb"}}).status_code == 400

    def handle(req):
        b = json.loads(req.content)
        if b["method"] == "getBalance":
            return httpx.Response(200, json={"result": {"value": 2_500_000_000}})
        return httpx.Response(200, json={"result": {"value": [{"account": {"data": {"parsed": {"info": {
            "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "tokenAmount": {"uiAmount": 42.0}}}}}}]}})
    monkeypatch.setattr(linked, "SOLANA_TRANSPORT", httpx.MockTransport(handle))
    a = client.post("/api/linked", json={"provider": "phantom", "values": {"address": "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"}}).json()
    h = {x["asset"]: x for x in a["snapshot"]["holdings"]}
    assert h["SOL"]["quantity"] == 2.5 and h["USDC"]["usd_value"] == 42.0 and a["snapshot"]["total_usd"] == 42.0


def test_futu_paper_by_default(client, monkeypatch):
    calls = {}

    class Ctx:
        def __init__(self, **kw):
            calls["ctx"] = kw

        def accinfo_query(self, trd_env):
            calls["env"] = trd_env
            return 0, [{"cash": 5000.0, "total_assets": 6500.0, "power": 10000.0}]

        def position_list_query(self, trd_env):
            return 0, [{"code": "US.NVDA", "qty": 10, "market_val": 1500.0, "nominal_price": 150.0, "cost_price": 140.0, "can_sell_qty": 10}]

        def close(self):
            calls["closed"] = True
    mod = types.SimpleNamespace(RET_OK=0, OpenSecTradeContext=Ctx, TrdEnv=types.SimpleNamespace(SIMULATE="SIM", REAL="REAL"),
                                SecurityFirm=types.SimpleNamespace(FUTUINC="FUTUINC"), TrdMarket=types.SimpleNamespace(US="US"))
    monkeypatch.setattr(linked, "FUTU_MODULE", mod)
    a = client.post("/api/linked", json={"provider": "futu", "values": {}}).json()
    assert calls["env"] == "SIM" and calls["closed"] and calls["ctx"]["port"] == 11111
    assert a["snapshot"]["total_usd"] == 6500.0


def test_copilot_can_read_linked_accounts(client, ex):
    client.post("/api/linked", json={"provider": "bybit", "values": {"apiKey": "k", "secret": "s"}})
    from app import db
    c = db.connect()
    try:
        out = copilot.READ["get_linked_accounts"](c, {})
    finally:
        c.close()
    assert out["total_usd_valued"] == 31000.0 and out["accounts"][0]["provider"] == "bybit"
    assert "s" not in [v for a in out["accounts"] for v in (a["fields"] or {}).values()]


def test_geo_blocked_exchange_says_so(client, ex):
    ex["fail"] = ccxt.ExchangeNotAvailable('binance GET https://testnet.binance.vision/api/v3/exchangeInfo 451  {"code":0,"msg":"Service unavailable from a restricted location"}')
    r = client.post("/api/linked", json={"provider": "binance", "values": {"apiKey": "k", "secret": "s"}})
    assert r.status_code == 451 and "blocks connections from your location" in r.json()["detail"] and "Binance.US" in r.json()["detail"]
    ex["fail"] = ccxt.RateLimitExceeded("bybit GET https://api-testnet.bybit.com/v5/x 403 Forbidden {}")
    r = client.post("/api/linked", json={"provider": "bybit", "values": {"apiKey": "k", "secret": "s"}})
    assert r.status_code == 451 and "refuse US connections" in r.json()["detail"]


def test_connections_overview_groups_methods_and_state(client, ex):
    client.post("/api/linked", json={"provider": "okx", "sandbox": True, "values": {"apiKey": "k", "secret": "s", "password": "p"}})
    d = client.get("/api/connections").json()
    by = {i["id"]: i for i in d["items"]}
    assert {g["id"] for g in d["groups"]} == {"stocks", "crypto", "wallets", "ai"}
    assert by["okx"]["state"] == "connected" and by["okx"]["group"] == "crypto"
    assert by["fidelity"]["state"] in ("planned", "unavailable") and by["fidelity"]["methods"] == []
    oauth = next(m for m in by["schwab"]["methods"] if m["kind"] == "oauth")
    assert oauth["available"] is False and "operator" in oauth["why"]
    assert any(m["kind"] == "oauth" for m in by["alpaca"]["methods"]) and any(m["kind"] == "api_key" for m in by["alpaca"]["methods"])
    assert by["phantom"]["methods"][0]["kind"] == "address"
    assert {m["kind"] for m in by["copilot"]["methods"]} >= {"plan", "api_key", "local"}
