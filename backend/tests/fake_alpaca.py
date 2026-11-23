"""A stateful in-memory stand-in for the Alpaca Paper REST API (the endpoints SwingDesk uses).

Implements order placement with OTO stop legs and bracket (take-profit + stop, OCO) legs, client_order_id uniqueness, held
quantity for open sells, cancels, nested order lookups, open-order listing,
positions and the account. Test helpers fill entries, trigger stops and fill
market-on-close orders, so the full broker lifecycle can be exercised offline.
"""
import json
import uuid

import httpx

GOOD_KEY, GOOD_SECRET = "PKTESTKEY1234", "secret-abc"
TERMINAL = {"filled", "canceled", "rejected", "expired"}


class FakeAlpaca:
    def __init__(self, equity=100000.0):
        self.account = {"equity": str(equity), "cash": str(equity), "buying_power": str(equity * 2), "status": "ACTIVE",
                        "trading_blocked": False, "account_blocked": False, "pattern_day_trader": False,
                        "daytrade_count": 0, "account_number": "PA0TEST"}
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}
        self.posts: list[dict] = []
        self.calls: list[str] = []

    # ---- helpers for tests ----
    def transport(self):
        return httpx.MockTransport(self.handle)

    def by_client(self, cid):
        return next(o for o in self.orders.values() if o["client_order_id"] == cid)

    def _apply_fill(self, o, qty, price):
        prev_q = int(o["filled_qty"])
        prev_avg = float(o["filled_avg_price"] or 0)
        new_q = prev_q + qty
        o["filled_avg_price"] = str(round((prev_avg * prev_q + price * qty) / new_q, 4))
        o["filled_qty"] = str(new_q)
        o["status"] = "filled" if new_q >= int(o["qty"]) else "partially_filled"
        p = self.positions.setdefault(o["symbol"], {"qty": 0, "avg": 0.0})
        if o["side"] == "buy":
            p["avg"] = (p["avg"] * p["qty"] + price * qty) / (p["qty"] + qty)
            p["qty"] += qty
        else:
            p["qty"] -= qty
        if p["qty"] == 0:
            del self.positions[o["symbol"]]

    def fill_entry(self, cid, price, qty=None):
        o = self.by_client(cid)
        self._apply_fill(o, qty or int(o["qty"]) - int(o["filled_qty"]), price)
        for lid in o.get("legs", []):
            leg = self.orders[lid]
            if leg["status"] == "held":
                leg["status"] = "new"     # stop becomes active once the parent fills

    def _oco(self, leg):
        """Bracket legs are one-cancels-other: filling one cancels its sibling."""
        parent = self.orders.get(leg.get("parent"))
        if parent and parent.get("order_class") == "bracket":
            for lid in parent["legs"]:
                if lid != leg["id"] and self.orders[lid]["status"] not in TERMINAL:
                    self.orders[lid]["status"] = "canceled"

    def trigger_stop(self, symbol, price):
        for o in list(self.orders.values()):
            if o["symbol"] == symbol and o["type"] == "stop" and o["status"] == "new":
                self._apply_fill(o, int(o["qty"]), price)
                self._oco(o)

    def fill_target(self, symbol, price):
        for o in list(self.orders.values()):
            if o["symbol"] == symbol and o["type"] == "limit" and o["side"] == "sell" and o["status"] == "new":
                self._apply_fill(o, int(o["qty"]), price)
                self._oco(o)

    def fill_sells(self, symbol, price, tif=None):
        for o in list(self.orders.values()):
            if o["symbol"] == symbol and o["side"] == "sell" and o["type"] == "market" and o["status"] not in TERMINAL \
                    and (tif is None or o["time_in_force"] == tif):
                self._apply_fill(o, int(o["qty"]) - int(o["filled_qty"]), price)

    def add_outside_position(self, symbol, qty, avg):
        self.positions[symbol] = {"qty": qty, "avg": avg}

    # ---- HTTP ----
    def _view(self, o):
        v = {k: val for k, val in o.items() if k != "legs"}
        v["legs"] = [dict(self.orders[l]) for l in o.get("legs", [])] or None
        return v

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(f"{req.method} {req.url.path}")
        if req.headers.get("APCA-API-KEY-ID") != GOOD_KEY or req.headers.get("APCA-API-SECRET-KEY") != GOOD_SECRET:
            return httpx.Response(401, json={"message": "unauthorized."})
        path, m = req.url.path, req.method
        if path == "/v2/account":
            return httpx.Response(200, json=self.account)
        if path == "/v2/positions":
            return httpx.Response(200, json=[{"symbol": s, "qty": str(p["qty"]), "avg_entry_price": str(p["avg"]),
                                              "market_value": str(p["qty"] * p["avg"]), "side": "long"} for s, p in self.positions.items()])
        if path == "/v2/orders:by_client_order_id":
            try:
                return httpx.Response(200, json=self._view(self.by_client(req.url.params["client_order_id"])))
            except StopIteration:
                return httpx.Response(404, json={"message": "order not found"})
        if path == "/v2/orders" and m == "GET":
            out = [self._view(o) for o in self.orders.values() if not o.get("parent")
                   and (o["status"] not in TERMINAL or any(self.orders[l]["status"] not in TERMINAL for l in o.get("legs", [])))]
            return httpx.Response(200, json=out)
        if path == "/v2/orders" and m == "POST":
            b = json.loads(req.content)
            self.posts.append(b)
            if any(o["client_order_id"] == b["client_order_id"] for o in self.orders.values()):
                return httpx.Response(422, json={"message": "client_order_id must be unique"})
            if b["side"] == "sell":
                held = sum(int(o["qty"]) - int(o["filled_qty"]) for o in self.orders.values()
                           if o["symbol"] == b["symbol"] and o["side"] == "sell" and o["status"] not in TERMINAL | {"held"})
                avail = self.positions.get(b["symbol"], {"qty": 0})["qty"] - held
                if int(b["qty"]) > avail:
                    return httpx.Response(403, json={"message": f"insufficient qty available for order (requested: {b['qty']}, available: {avail})"})
            oid = uuid.uuid4().hex[:10]
            o = {"id": oid, "client_order_id": b["client_order_id"], "symbol": b["symbol"], "qty": b["qty"], "side": b["side"],
                 "type": b["type"], "time_in_force": b["time_in_force"], "status": "accepted", "filled_qty": "0",
                 "filled_avg_price": None, "order_class": b.get("order_class", "simple"), "legs": []}
            self.orders[oid] = o
            if b.get("order_class") == "oto" and b.get("stop_loss"):
                lid = uuid.uuid4().hex[:10]
                self.orders[lid] = {"id": lid, "client_order_id": uuid.uuid4().hex, "symbol": b["symbol"], "qty": b["qty"],
                                    "side": "sell", "type": "stop", "stop_price": b["stop_loss"]["stop_price"],
                                    "time_in_force": b["time_in_force"], "status": "held", "filled_qty": "0",
                                    "filled_avg_price": None, "parent": oid, "legs": []}
                o["legs"] = [lid]
            if b.get("order_class") == "bracket":
                legs = []
                for typ, key, px in (("limit", "limit_price", b["take_profit"]["limit_price"]), ("stop", "stop_price", b["stop_loss"]["stop_price"])):
                    lid = uuid.uuid4().hex[:10]
                    self.orders[lid] = {"id": lid, "client_order_id": uuid.uuid4().hex, "symbol": b["symbol"], "qty": b["qty"],
                                        "side": "sell", "type": typ, key: px, "time_in_force": b["time_in_force"], "status": "held",
                                        "filled_qty": "0", "filled_avg_price": None, "parent": oid, "legs": []}
                    legs.append(lid)
                o["legs"] = legs
            return httpx.Response(200, json=self._view(o))
        if path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[-1]
            o = self.orders.get(oid)
            if not o:
                return httpx.Response(404, json={"message": "order not found"})
            if m == "GET":
                return httpx.Response(200, json=self._view(o))
            if m == "DELETE":
                if o["status"] in TERMINAL:
                    return httpx.Response(422, json={"message": f"order is already {o['status']}"})
                o["status"] = "canceled"
                for l in o.get("legs", []):
                    if self.orders[l]["status"] not in TERMINAL:
                        self.orders[l]["status"] = "canceled"
                return httpx.Response(204)
        return httpx.Response(404, json={"message": f"unhandled {m} {path}"})
