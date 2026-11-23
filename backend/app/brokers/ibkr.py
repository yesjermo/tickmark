"""Interactive Brokers adapter over the Client Portal Web API (local gateway).

How a user connects: download IBKR's Client Portal Gateway, start it, log in at
https://localhost:5000 in a browser (IBKR handles the password and two-factor),
then click "Check connection" in Tickmark. No IBKR credentials are stored by
Tickmark; the gateway holds the session.

Safety: paper accounts (IDs starting "DU") by default. A live account is accepted only after
real-money trading was enabled for IBKR through the live switch (app/live_gate.py).

API facts, checked 2026-11-22 against IBKR Campus "Trading Web API":
  CONFIRMED  POST /iserver/account/{accountId}/orders  body {"orders": [ticket, ...]} with
             conid, orderType, side, tif, quantity, price (case-sensitive keys)
  CONFIRMED  reply flow: response [{"id": ..., "message": [...]}] -> POST /iserver/reply/{id} {"confirmed": true}
  CONFIRMED  success response [{"order_id": ..., "order_status": "Submitted", ...}]
  CONFIRMED  DELETE /iserver/account/{accountId}/order/{orderId} cancels
  CONFIRMED  GET /iserver/account/orders -> {"orders": [{orderId, ticker, side, status, filledQuantity,
             remainingQuantity, totalSize, avgPrice (string), order_ref, timeInForce, orderType}], ...}
  CONFIRMED  GET /portfolio/accounts -> [{"id": ...}]; GET /portfolio/{accountId}/ledger -> {"BASE": {netliquidationvalue, cashbalance}}
  CONFIRMED  session fields: connected, authenticated, established, competing; paper examples use DU accounts
  UNCONFIRMED (docs section "coming soon" or not on that page): bracket child via cOID/parentId,
             STP stop price in "price", GET /iserver/auth/status path, GET /iserver/secdef/search?symbol=,
             GET /portfolio/{accountId}/positions/0 fields, /portfolio/{accountId}/summary buyingpower,
             market-on-close order type (so planned exits use a market order instead).
Before trusting this with a real paper account, compare against the gateway's own API docs.
"""
from __future__ import annotations

import sys
from urllib.parse import urlparse

import httpx

from .. import db
from .base import BrokerAdapter, BrokerError, BrokerResult, OrderRequest

DEFAULT_GATEWAY = "https://localhost:5000/v1/api"
TRANSPORT: httpx.BaseTransport | None = None   # injectable for tests
MAX_REPLIES = 5

STATUS_MAP = {
    "Submitted": "pending", "PreSubmitted": "pending", "PendingSubmit": "pending", "PendingCancel": "pending",
    "ApiPending": "pending", "Filled": "filled", "Cancelled": "canceled", "ApiCancelled": "canceled",
    "Inactive": "rejected",
}


def settings() -> dict:
    return {"gateway_url": DEFAULT_GATEWAY, "account_id": None, **(db.get_setting("ibkr", {}) or {})}


def save_settings(gateway_url: str | None, account_id: str | None) -> dict:
    s = settings()
    if gateway_url:
        u = urlparse(gateway_url)
        if u.scheme != "https" or not u.hostname:
            raise ValueError("Gateway URL must be https://host:port/v1/api")
        s["gateway_url"] = gateway_url.rstrip("/")
    if account_id is not None:
        s["account_id"] = account_id.strip().upper() or None
    db.set_setting("ibkr", s)
    return s


def configured() -> bool:
    return bool(settings().get("account_id"))


def _is_local(url: str) -> bool:
    return (urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1")


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class IBKRBroker(BrokerAdapter):
    name = "ibkr"
    capabilities = {"paper": True, "attached_stop": True, "market_on_close": False, "real_money": False, "bracket": False}

    def __init__(self, transport: httpx.BaseTransport | None = None, gateway_url: str | None = None,
                 account_id: str | None = None, require_account: bool = True):
        s = settings()
        self.base = (gateway_url or s["gateway_url"]).rstrip("/")
        self.account_id = account_id or s.get("account_id")
        if require_account:
            if not self.account_id:
                raise BrokerError("No IBKR account selected. Log in to the gateway and pick a paper account in the Broker tab.")
            from .. import live_gate
            if not self.account_id.startswith("DU") and not (live_gate.enabled() and db.get_setting("live_broker") == "ibkr"):
                raise BrokerError(f"IBKR account {self.account_id} is not a paper account (paper IDs start with DU). "
                                  "Live IBKR accounts trade only after real-money trading is enabled for IBKR (Broker -> Live trading).")
        # The gateway uses a self-signed certificate on localhost; verify certificates anywhere else.
        self.client = httpx.Client(base_url=self.base, transport=transport or TRANSPORT,
                                   timeout=httpx.Timeout(15, connect=4), verify=not _is_local(self.base))

    # ---- session ----
    def _req(self, method, path, **kw) -> httpx.Response:
        try:
            r = self.client.request(method, path, **kw)
        except httpx.TransportError as exc:
            raise BrokerError(self._unreachable_hint(exc))
        if r.status_code == 401:
            raise BrokerError("IBKR gateway session is not authenticated. Log in at the gateway page, then retry.")
        if r.status_code >= 400:
            raise BrokerError(f"IBKR error {r.status_code}: {r.text[:200]}")
        return r

    def _unreachable_hint(self, exc) -> str:
        port = urlparse(self.base).port
        msg = f"Can't reach the IBKR gateway at {self.base}. Is it running and logged in? ({type(exc).__name__})"
        if port == 5000 and sys.platform == "darwin":
            msg += (" On macOS, port 5000 is usually taken by AirPlay Receiver, so something other than IBKR may be answering. "
                    "Either turn off System Settings > General > AirDrop & Handoff > AirPlay Receiver, or set listenPort "
                    "in the gateway's root/conf.yaml to another port (e.g. 5001) and update the Gateway URL here.")
        return msg

    def session(self) -> dict:
        d = self._req("POST", "/iserver/auth/status").json()
        return {k: bool(d.get(k)) for k in ("authenticated", "connected", "established", "competing")}

    def list_accounts(self) -> list[dict]:
        out = []
        for a in self._req("GET", "/portfolio/accounts").json():
            aid = a.get("id") or a.get("accountId")
            out.append({"id": aid, "paper": str(aid).startswith("DU"), "desc": a.get("desc") or a.get("accountTitle")})
        return out

    def _prime(self):
        # Trading endpoints need the brokerage account list loaded in this session.
        self._req("GET", "/iserver/accounts")

    # ---- contracts ----
    def conid(self, symbol: str) -> int:
        c = db.connect()
        try:
            db.init_schema(c)
            cache = db.kv_get(c, "ibkr_conids", {}) or {}
        finally:
            c.close()
        if symbol in cache:
            return cache[symbol]
        rows = self._req("GET", "/iserver/secdef/search", params={"symbol": symbol}).json() or []
        pick = next((r for r in rows if r.get("conid") and (r.get("description") in (None, "NASDAQ", "NYSE", "ARCA", "AMEX")
                                                           or "STK" in str(r.get("sections", "")))), rows[0] if rows else None)
        if not pick or not pick.get("conid"):
            raise BrokerError(f"IBKR has no stock contract for {symbol}")
        cid = int(pick["conid"])
        c = db.connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            cache[symbol] = cid
            db.kv_set(c, "ibkr_conids", cache)
            c.execute("COMMIT")
        finally:
            c.close()
        return cid

    # ---- orders ----
    def _place(self, tickets: list[dict]) -> list[dict]:
        self._prime()
        resp = self._req("POST", f"/iserver/account/{self.account_id}/orders", json={"orders": tickets}).json()
        for _ in range(MAX_REPLIES):
            if isinstance(resp, dict) and resp.get("error"):
                raise BrokerError(str(resp["error"]))
            if isinstance(resp, list) and resp and "id" in resp[0] and "message" in resp[0] and "order_id" not in resp[0]:
                # IBKR asks us to acknowledge a precaution message before the order goes live.
                resp = self._req("POST", f"/iserver/reply/{resp[0]['id']}", json={"confirmed": True}).json()
                continue
            return resp if isinstance(resp, list) else [resp]
        raise BrokerError("IBKR kept asking for order confirmations; order not placed")

    def _orders(self) -> list[dict]:
        d = self._req("GET", "/iserver/account/orders").json()
        return d.get("orders", []) if isinstance(d, dict) else (d or [])

    @staticmethod
    def _status(o: dict) -> str:
        st = STATUS_MAP.get(o.get("status") or o.get("order_status"), "pending")
        filled, remaining = _f(o.get("filledQuantity")) or 0, _f(o.get("remainingQuantity"))
        if st == "pending" and filled > 0 and (remaining or 0) > 0:
            return "partially_filled"
        return st

    def _result(self, o: dict, all_orders: list[dict] | None = None) -> BrokerResult:
        legs = []
        ref = o.get("order_ref")
        for ch in (all_orders or []):
            if ref and ch.get("order_ref") == f"{ref}:stop":
                legs.append({"id": str(ch.get("orderId")), "type": "stop", "side": "sell",
                             "stop_price": _f(ch.get("price") or ch.get("auxPrice")), "status": self._status(ch),
                             "raw_status": ch.get("status"), "filled_qty": int(_f(ch.get("filledQuantity")) or 0),
                             "avg_fill_price": _f(ch.get("avgPrice"))})
        return BrokerResult(broker_order_id=str(o.get("orderId") or o.get("order_id")), status=self._status(o),
                            status_reason=o.get("status") or o.get("order_status"),
                            filled_qty=int(_f(o.get("filledQuantity")) or 0), avg_fill_price=_f(o.get("avgPrice")), legs=legs)

    def submit(self, req: OrderRequest) -> BrokerResult:
        existing = next((o for o in self._orders() if o.get("order_ref") == req.client_order_id), None)
        if existing:   # same client order id already at IBKR: never place it twice
            return self._result(existing, self._orders())
        conid = self.conid(req.symbol)
        parent = {"acctId": self.account_id, "conid": conid, "cOID": req.client_order_id, "side": "BUY",
                  "orderType": "MKT" if req.order_type == "market" else "LMT", "quantity": req.qty, "tif": "GTC"}
        if req.order_type == "limit":
            parent["price"] = round(req.limit_price, 2)
        tickets = [parent]
        if req.stop_price is not None:
            tickets.append({"acctId": self.account_id, "conid": conid, "cOID": f"{req.client_order_id}:stop",
                            "parentId": req.client_order_id, "side": "SELL", "orderType": "STP",
                            "price": round(req.stop_price, 2), "quantity": req.qty, "tif": "GTC"})
        try:
            placed = self._place(tickets)
        except BrokerError as exc:
            return BrokerResult(None, "rejected", str(exc))
        first = placed[0]
        orders = self._orders()
        live = next((o for o in orders if str(o.get("orderId")) == str(first.get("order_id"))), None)
        if live:
            return self._result(live, orders)
        legs = [{"id": str(p.get("order_id")), "type": "stop", "side": "sell", "stop_price": req.stop_price,
                 "status": STATUS_MAP.get(p.get("order_status"), "pending"), "raw_status": p.get("order_status"),
                 "filled_qty": 0, "avg_fill_price": None} for p in placed[1:]]
        return BrokerResult(str(first.get("order_id")), STATUS_MAP.get(first.get("order_status"), "pending"),
                            first.get("order_status"), legs=legs)

    def sell(self, symbol: str, qty: int, client_order_id: str, at_close: bool = False) -> BrokerResult:
        # Market-on-close isn't confirmed for this API, so exits are market orders (sent at 15:40 ET by the scheduler).
        existing = next((o for o in self._orders() if o.get("order_ref") == client_order_id), None)
        if existing:
            return self._result(existing)
        try:
            placed = self._place([{"acctId": self.account_id, "conid": self.conid(symbol), "cOID": client_order_id,
                                   "side": "SELL", "orderType": "MKT", "quantity": qty, "tif": "DAY"}])
        except BrokerError as exc:
            return BrokerResult(None, "rejected", str(exc))
        p = placed[0]
        return BrokerResult(str(p.get("order_id")), STATUS_MAP.get(p.get("order_status"), "pending"), p.get("order_status"))

    def get(self, broker_order_id: str) -> BrokerResult:
        orders = self._orders()
        o = next((x for x in orders if str(x.get("orderId")) == str(broker_order_id)), None)
        if not o:
            raise BrokerError(f"IBKR order {broker_order_id} not in this session's order list")
        return self._result(o, orders)

    def get_by_client_id(self, client_order_id: str) -> BrokerResult:
        orders = self._orders()
        o = next((x for x in orders if x.get("order_ref") == client_order_id), None)
        if not o:
            raise BrokerError(f"No IBKR order with reference {client_order_id}")
        return self._result(o, orders)

    def cancel(self, broker_order_id: str) -> None:
        self._req("DELETE", f"/iserver/account/{self.account_id}/order/{broker_order_id}")

    def open_orders(self) -> list[dict]:
        orders = self._orders()
        out = []
        for o in orders:
            if STATUS_MAP.get(o.get("status"), "pending") != "pending":
                continue
            if str(o.get("order_ref", "")).endswith(":stop"):
                # child stop: report it on its own as a sell-side protective order
                out.append({"symbol": o.get("ticker"), "side": "sell", "result": self._result(o)})
                continue
            out.append({"symbol": o.get("ticker"), "side": str(o.get("side", "")).lower(), "result": self._result(o, orders)})
        return out

    # ---- account ----
    def positions(self) -> dict[str, dict]:
        rows = self._req("GET", f"/portfolio/{self.account_id}/positions/0").json() or []
        out = {}
        for p in rows:
            sym = p.get("ticker") or p.get("contractDesc")
            qty = int(_f(p.get("position")) or 0)
            if sym and qty:
                out[sym] = {"qty": qty, "avg_entry_price": _f(p.get("avgCost") or p.get("avgPrice")),
                            "market_value": _f(p.get("mktValue")), "side": "long" if qty > 0 else "short"}
        return out

    def account(self) -> dict:
        sess = self.session()
        ledger = self._req("GET", f"/portfolio/{self.account_id}/ledger").json() or {}
        base = ledger.get("BASE") or ledger.get("USD") or {}
        equity = _f(base.get("netliquidationvalue")) or 0.0
        cash = _f(base.get("cashbalance")) or 0.0
        bp = None
        try:
            summ = self._req("GET", f"/portfolio/{self.account_id}/summary").json() or {}
            bp = _f((summ.get("buyingpower") or {}).get("amount"))
        except BrokerError:
            pass
        ok = sess["authenticated"] and sess["connected"]
        return {"equity": equity, "cash": cash, "buying_power": bp if bp is not None else cash,
                "status": "ACTIVE" if ok else "NOT_AUTHENTICATED", "trading_blocked": not ok, "account_blocked": False,
                "pattern_day_trader": False, "daytrade_count": None, "account_number": self.account_id,
                "competing_session": sess["competing"]}
