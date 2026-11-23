"""Alpaca Paper Trading adapter.

Credentials come from the server keychain or environment (app/credentials.py) and
never reach the browser. The base URL is pinned to the paper endpoint; this
prototype refuses to talk to a live-trading URL.
"""
from __future__ import annotations

import os

import httpx

from .. import credentials
from .base import BrokerAdapter, BrokerError, BrokerResult, OrderRequest

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
TRANSPORT: httpx.BaseTransport | None = None  # injectable for tests

STATUS_MAP = {
    "new": "pending", "accepted": "pending", "pending_new": "pending", "accepted_for_bidding": "pending",
    "held": "pending", "calculated": "pending", "pending_replace": "pending", "replaced": "pending",
    "partially_filled": "partially_filled", "filled": "filled", "done_for_day": "partially_filled",
    "canceled": "canceled", "pending_cancel": "pending", "expired": "canceled", "stopped": "filled",
    "rejected": "rejected", "suspended": "rejected",
}


def current_env() -> str:
    from .. import db
    return db.environment()


def configured(env: str | None = None) -> bool:
    k, s, _ = credentials.get_alpaca(env or current_env())
    return bool(k and s)


def _f(v):
    return float(v) if v not in (None, "") else None


class AlpacaPaperBroker(BrokerAdapter):
    name = "alpaca_paper"
    capabilities = {"paper": True, "attached_stop": True, "market_on_close": True, "real_money": True, "bracket": True}

    def __init__(self, transport: httpx.BaseTransport | None = None, key: str | None = None, secret: str | None = None,
                 env: str | None = None, key_test: bool = False):
        env = env or current_env()
        if env == "real":
            # Real money only after the live switch was explicitly enabled.
            # key_test lets the gate test live keys with a read-only account call before enabling.
            from .. import live_gate
            if not (live_gate.enabled() or key_test):
                raise BrokerError("Real-money trading is not enabled (see Broker -> Live trading)")
            base = LIVE_URL
        else:
            base = os.environ.get("ALPACA_BASE_URL", PAPER_URL)
            if "paper" not in base:
                raise BrokerError("Refusing a non-paper Alpaca URL in paper mode")
        if not (key and secret):
            key, secret, _ = credentials.get_alpaca(env)
        self.env, self.base_url = env, base
        if not (key and secret):
            raise BrokerError(f"Alpaca {'live' if env == 'real' else 'paper'} credentials are not configured on the server")
        self.client = httpx.Client(base_url=base, transport=transport or TRANSPORT, timeout=10,
                                   headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    @staticmethod
    def _result(data: dict) -> BrokerResult:
        status = STATUS_MAP.get(data.get("status", ""), "pending")
        legs = [{"id": l.get("id"), "type": l.get("type"), "side": l.get("side"), "stop_price": _f(l.get("stop_price")),
                 "limit_price": _f(l.get("limit_price")),
                 "status": STATUS_MAP.get(l.get("status", ""), "pending"), "raw_status": l.get("status"),
                 "filled_qty": int(float(l.get("filled_qty") or 0)), "avg_fill_price": _f(l.get("filled_avg_price"))}
                for l in (data.get("legs") or [])]
        return BrokerResult(broker_order_id=data.get("id"), status=status, status_reason=data.get("status"),
                            filled_qty=int(float(data.get("filled_qty") or 0)), avg_fill_price=_f(data.get("filled_avg_price")),
                            legs=legs)

    def _check(self, r: httpx.Response):
        if r.status_code in (401, 403):
            raise BrokerError("Alpaca rejected the API keys (unauthorized or forbidden)")
        if r.status_code >= 400:
            raise BrokerError(f"Alpaca error {r.status_code}: {r.text[:200]}")
        return r

    # ---- orders ----
    def submit(self, req: OrderRequest) -> BrokerResult:
        # GTC when a stop is attached: with "day" the protective stop leg would expire after the first session,
        # leaving a swing position unprotected overnight.
        body = {"symbol": req.symbol, "qty": str(req.qty), "side": req.side, "type": req.order_type,
                "time_in_force": "gtc" if req.stop_price is not None else "day", "client_order_id": req.client_order_id}
        if req.order_type == "limit":
            body["limit_price"] = f"{req.limit_price:.2f}"
        if req.stop_price is not None:
            body["order_class"] = "oto"
            body["stop_loss"] = {"stop_price": f"{req.stop_price:.2f}"}
        r = self.client.post("/v2/orders", json=body)
        if r.status_code == 422 and "client_order_id" in r.text:
            # Alpaca also enforces client_order_id uniqueness: fetch the original instead of double-submitting.
            return self.get_by_client_id(req.client_order_id)
        if r.status_code in (403, 422):
            return BrokerResult(None, "rejected", r.json().get("message", r.text))
        self._check(r)
        return self._result(r.json())

    def submit_bracket(self, req: OrderRequest, take_profit: float) -> BrokerResult:
        """Day-trade entry: a bracket (entry + take-profit limit + stop, one-cancels-the-other), time in force DAY,
        so nothing carries overnight even if Tickmark's 15:55 flatten were missed."""
        body = {"symbol": req.symbol, "qty": str(req.qty), "side": "buy", "type": req.order_type, "time_in_force": "day",
                "order_class": "bracket", "client_order_id": req.client_order_id,
                "take_profit": {"limit_price": f"{take_profit:.2f}"}, "stop_loss": {"stop_price": f"{req.stop_price:.2f}"}}
        if req.order_type == "limit":
            body["limit_price"] = f"{req.limit_price:.2f}"
        r = self.client.post("/v2/orders", json=body)
        if r.status_code == 422 and "client_order_id" in r.text:
            return self.get_by_client_id(req.client_order_id)
        if r.status_code in (403, 422):
            return BrokerResult(None, "rejected", r.json().get("message", r.text))
        self._check(r)
        return self._result(r.json())

    def sell(self, symbol: str, qty: int, client_order_id: str, at_close: bool = False) -> BrokerResult:
        """Market sell. at_close=True sends a market-on-close order (time_in_force 'cls'; Alpaca needs it before 15:50 ET)."""
        body = {"symbol": symbol, "qty": str(qty), "side": "sell", "type": "market",
                "time_in_force": "cls" if at_close else "day", "client_order_id": client_order_id}
        r = self.client.post("/v2/orders", json=body)
        if r.status_code == 422 and "client_order_id" in r.text:
            return self.get_by_client_id(client_order_id)
        if r.status_code in (403, 422):
            return BrokerResult(None, "rejected", r.json().get("message", r.text))
        self._check(r)
        return self._result(r.json())

    def get(self, broker_order_id: str) -> BrokerResult:
        return self._result(self._check(self.client.get(f"/v2/orders/{broker_order_id}", params={"nested": "true"})).json())

    def get_by_client_id(self, client_order_id: str) -> BrokerResult:
        r = self.client.get("/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})
        return self._result(self._check(r).json())

    def cancel(self, broker_order_id: str) -> None:
        r = self.client.delete(f"/v2/orders/{broker_order_id}")
        if r.status_code not in (200, 204, 422):   # 422 = already filled/canceled
            raise BrokerError(f"Cancel failed: {r.status_code} {r.text[:200]}")

    def open_orders(self) -> list[dict]:
        """Open orders with their legs: [{symbol, side, result}]."""
        r = self._check(self.client.get("/v2/orders", params={"status": "open", "nested": "true", "limit": 500}))
        return [{"symbol": o.get("symbol"), "side": o.get("side"), "result": self._result(o)} for o in r.json()]

    # ---- account ----
    def positions(self) -> dict[str, dict]:
        r = self._check(self.client.get("/v2/positions"))
        return {p["symbol"]: {"qty": int(float(p["qty"])), "avg_entry_price": _f(p.get("avg_entry_price")),
                              "market_value": _f(p.get("market_value")), "side": p.get("side")} for p in r.json()}

    def account(self) -> dict:
        a = self._check(self.client.get("/v2/account")).json()
        return {"equity": float(a["equity"]), "cash": float(a["cash"]), "buying_power": float(a["buying_power"]),
                "status": a.get("status"), "trading_blocked": bool(a.get("trading_blocked")),
                "account_blocked": bool(a.get("account_blocked")), "pattern_day_trader": bool(a.get("pattern_day_trader")),
                "daytrade_count": int(a.get("daytrade_count") or 0), "account_number": a.get("account_number")}
