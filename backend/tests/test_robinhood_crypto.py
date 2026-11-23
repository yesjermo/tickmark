"""Robinhood Crypto order adapter: signed requests (including the body), symbol mapping, deterministic UUID client ids."""
import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.brokers import robinhood_crypto as rh


@pytest.fixture
def server(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    seed = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = priv.public_key()
    st = {"orders": {}, "posts": []}

    def handle(req: httpx.Request):
        path, body = req.url.raw_path.decode(), req.content.decode()
        pub.verify(base64.b64decode(req.headers["x-signature"]),
                   f"{req.headers['x-api-key']}{req.headers['x-timestamp']}{path}{req.method}{body}".encode())
        if path.startswith("/api/v1/crypto/trading/trading_pairs/"):
            return httpx.Response(200, json={"results": [{"symbol": "BTC-USD", "asset_code": "BTC", "quote_code": "USD", "status": "tradable",
                                                          "min_order_size": "0.000001", "asset_increment": "0.00000001", "quote_increment": "0.01"}], "next": None})
        if path.startswith("/api/v1/crypto/marketdata/best_bid_ask/"):
            return httpx.Response(200, json={"results": [{"symbol": "BTC-USD", "price": "60000", "bid_inclusive_of_sell_spread": "59950",
                                                          "ask_inclusive_of_buy_spread": "60050"}]})
        if path == "/api/v1/crypto/trading/orders/" and req.method == "POST":
            b = json.loads(body)
            st["posts"].append(b)
            if any(o["client_order_id"] == b["client_order_id"] for o in st["orders"].values()):
                return httpx.Response(400, json={"errors": ["duplicate client_order_id"]})
            oid = f"o{len(st['orders']) + 1}"
            st["orders"][oid] = {"id": oid, "client_order_id": b["client_order_id"], "symbol": b["symbol"], "side": b["side"],
                                 "type": b["type"], "state": "open", "filled_asset_quantity": "0", "average_price": None, "executions": []}
            return httpx.Response(201, json=st["orders"][oid])
        if path.startswith("/api/v1/crypto/trading/orders/") and path.endswith("/cancel/"):
            st["orders"][path.split("/")[-3]]["state"] = "canceled"
            return httpx.Response(200, content=b"")
        if path.startswith("/api/v1/crypto/trading/orders/"):
            return httpx.Response(200, json=st["orders"][path.split("/")[-2]])
        return httpx.Response(404)
    monkeypatch.setattr(rh, "TRANSPORT", httpx.MockTransport(handle))
    return st, base64.b64encode(seed).decode()


def test_orders_are_signed_mapped_and_idempotent(server):
    st, key = server
    ex = rh.RobinhoodCrypto("rh-api-x", key)
    assert "BTC/USD" in ex.load_markets() and ex.amount_to_precision("BTC/USD", 0.0016666666666) == "0.00166666"
    t = ex.fetch_ticker("BTC/USD")
    assert t["ask"] == 60050.0 and t["bid"] == 59950.0
    o = ex.create_order("BTC/USD", "market", "buy", "0.00166666", None, {"clientOrderId": "cvabc"})
    body = st["posts"][-1]
    assert body["symbol"] == "BTC-USD" and body["market_order_config"] == {"asset_quantity": "0.00166666"}
    assert body["client_order_id"] == rh.RobinhoodCrypto._uuid("cvabc") and o["status"] == "open"
    import ccxt
    with pytest.raises(ccxt.InvalidOrder):                                    # same preview -> same UUID -> refused
        ex.create_order("BTC/USD", "market", "buy", "0.00166666", None, {"clientOrderId": "cvabc"})
    s = ex.create_stop_order("BTC/USD", "market", "sell", "0.00166666", None, 57000, {"clientOrderId": "cvabcs1"})
    assert st["posts"][-1]["type"] == "stop_loss" and st["posts"][-1]["stop_loss_order_config"]["stop_price"] == "57000"
    st["orders"][o["id"]].update(state="filled", filled_asset_quantity="0.00166666", average_price="60040")
    f = ex.fetch_order(o["id"])
    assert f["status"] == "closed" and f["average"] == 60040.0
    assert ex.cancel_order(s["id"])["status"] == "canceled"
    with pytest.raises(ccxt.NotSupported):
        ex.set_sandbox_mode(True)
