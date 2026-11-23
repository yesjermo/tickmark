"""Robinhood Crypto Trading API, wrapped in the small ccxt-like interface crypto_trading.py uses.

Official API (no sandbox): every request is signed with the user's Ed25519 private key:
  x-api-key, x-timestamp, x-signature = base64(Ed25519.sign(api_key + timestamp + path + method + body))
Symbols are "BTC-USD" at Robinhood and "BTC/USD" here. Client order ids must be UUIDs, so SwingDesk's
preview id is mapped to a deterministic UUID (uuid5): the same preview always maps to the same id, which keeps
duplicate protection at the exchange.

Built from Robinhood's published Crypto Trading API docs; tested against a fake server only.
"""
from __future__ import annotations

import base64
import json
import math
import time
import uuid

import ccxt
import httpx

BASE = "https://trading.robinhood.com"
TRANSPORT = None          # tests
NS = uuid.UUID("6f2a8f55-7f4a-4a5e-9a2b-5c1d3e0f9a10")
STATE = {"open": "open", "pending": "open", "partially_filled": "open", "filled": "closed", "canceled": "canceled",
         "failed": "rejected"}


def _rh(sym: str) -> str:
    return sym.replace("/", "-")


def _cc(sym: str) -> str:
    return sym.replace("-", "/")


class RobinhoodCrypto:
    def __init__(self, api_key: str, private_key_b64: str):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        try:
            self.key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64)[:32])
        except Exception:
            raise ccxt.AuthenticationError("The private key must be the base64 Ed25519 private key you generated for Robinhood.")
        self.api_key = api_key
        self.http = httpx.Client(base_url=BASE, transport=TRANSPORT, timeout=15)
        self.markets: dict = {}

    def set_sandbox_mode(self, on):
        if on:
            raise ccxt.NotSupported("Robinhood Crypto has no sandbox")

    # ---- transport ----
    def _req(self, method: str, path: str, body: dict | None = None):
        ts = str(int(time.time()))
        raw = json.dumps(body, separators=(",", ":")) if body is not None else ""
        sig = base64.b64encode(self.key.sign(f"{self.api_key}{ts}{path}{method}{raw}".encode())).decode()
        headers = {"x-api-key": self.api_key, "x-timestamp": ts, "x-signature": sig, "Content-Type": "application/json"}
        try:
            r = self.http.request(method, path, content=raw or None, headers=headers)
        except httpx.HTTPError as exc:
            raise ccxt.NetworkError(f"Robinhood: {exc}")
        if r.status_code in (401, 403):
            raise ccxt.AuthenticationError(f"Robinhood rejected the key or signature ({r.status_code})")
        if r.status_code == 400:
            raise ccxt.InvalidOrder(f"Robinhood: {r.text[:300]}")
        if r.status_code == 404:
            raise ccxt.OrderNotFound(f"Robinhood: {r.text[:200]}")
        if r.status_code >= 400:
            raise ccxt.ExchangeError(f"Robinhood {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    def _pages(self, path: str) -> list:
        out = []
        while path:
            d = self._req("GET", path)
            out += d.get("results", [])
            nxt = d.get("next")
            path = nxt.replace(BASE, "") if nxt else None
        return out

    # ---- markets / prices / balances ----
    def load_markets(self):
        if not self.markets:
            for p in self._pages("/api/v1/crypto/trading/trading_pairs/"):
                self.markets[_cc(p["symbol"])] = {
                    "symbol": _cc(p["symbol"]), "base": p["asset_code"], "quote": p["quote_code"], "spot": True,
                    "active": p.get("status") == "tradable",
                    "limits": {"amount": {"min": float(p.get("min_order_size") or 0) or None, "max": float(p.get("max_order_size") or 0) or None}},
                    "asset_increment": float(p.get("asset_increment") or 1e-8), "quote_increment": float(p.get("quote_increment") or 0.01)}
        return self.markets

    def amount_to_precision(self, symbol, amount):
        inc = self.load_markets()[symbol]["asset_increment"]
        n = max(0, -int(math.floor(math.log10(inc))))
        return f"{math.floor(float(amount) / inc) * inc:.{n}f}"

    def price_to_precision(self, symbol, price):
        inc = self.load_markets()[symbol]["quote_increment"]
        n = max(0, -int(math.floor(math.log10(inc))))
        return f"{round(float(price) / inc) * inc:.{n}f}"

    def fetch_ticker(self, symbol):
        d = self._req("GET", f"/api/v1/crypto/marketdata/best_bid_ask/?symbol={_rh(symbol)}")
        r = (d.get("results") or [{}])[0]
        f = lambda k: float(r[k]) if r.get(k) not in (None, "") else None  # noqa: E731
        return {"bid": f("bid_inclusive_of_sell_spread"), "ask": f("ask_inclusive_of_buy_spread"), "last": f("price")}

    def fetch_balance(self):
        a = self._req("GET", "/api/v1/crypto/trading/accounts/")
        free, total = {}, {}
        cur = a.get("buying_power_currency", "USD")
        free[cur] = total[cur] = float(a.get("buying_power") or 0)
        for h in self._pages("/api/v1/crypto/trading/holdings/"):
            total[h["asset_code"]] = float(h.get("total_quantity") or 0)
            free[h["asset_code"]] = float(h.get("quantity_available_for_trading") or 0)
        return {"free": free, "total": total}

    # ---- orders ----
    @staticmethod
    def _uuid(client_id: str | None) -> str:
        return str(uuid.uuid5(NS, client_id)) if client_id else str(uuid.uuid4())

    def _order(self, o: dict) -> dict:
        filled = float(o.get("filled_asset_quantity") or 0)
        avg = float(o["average_price"]) if o.get("average_price") else None
        fees = sum(float(e.get("fee") or 0) for e in o.get("executions") or [] if isinstance(e, dict))
        return {"id": o.get("id"), "clientOrderId": o.get("client_order_id"), "symbol": _cc(o.get("symbol", "")), "side": o.get("side"),
                "type": o.get("type"), "status": STATE.get(o.get("state"), "open"), "filled": filled, "average": avg,
                "cost": filled * avg if avg else 0.0, "fee": {"cost": fees, "currency": "USD"} if fees else None}

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        cid = self._uuid((params or {}).get("clientOrderId"))
        body = {"client_order_id": cid, "side": side, "symbol": _rh(symbol), "type": type_}
        if type_ == "market":
            body["market_order_config"] = {"asset_quantity": str(amount)}
        elif type_ == "limit":
            body["limit_order_config"] = {"asset_quantity": str(amount), "limit_price": str(price), "time_in_force": "gtc"}
        else:
            raise ccxt.InvalidOrder(f"order type {type_} not supported")
        return self._order(self._req("POST", "/api/v1/crypto/trading/orders/", body))

    def create_stop_order(self, symbol, type_, side, amount, price=None, trigger=None, params=None):
        cid = self._uuid((params or {}).get("clientOrderId"))
        body = {"client_order_id": cid, "side": side, "symbol": _rh(symbol), "type": "stop_loss",
                "stop_loss_order_config": {"asset_quantity": str(amount), "stop_price": str(trigger), "time_in_force": "gtc"}}
        return self._order(self._req("POST", "/api/v1/crypto/trading/orders/", body))

    def fetch_order(self, oid, symbol=None, params=None):
        return self._order(self._req("GET", f"/api/v1/crypto/trading/orders/{oid}/"))

    def cancel_order(self, oid, symbol=None, params=None):
        self._req("POST", f"/api/v1/crypto/trading/orders/{oid}/cancel/")
        return self.fetch_order(oid)
