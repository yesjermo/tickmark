"""A stateful stand-in for IBKR's Client Portal gateway (the endpoints SwingDesk uses).

Mirrors the documented shapes (orders body {"orders": [...]}, the order-reply
confirmation step, /iserver/account/orders fields, ledger) plus the unconfirmed ones
the adapter assumes (bracket child via cOID/parentId, positions, secdef search).
"""
import json

import httpx

ACCOUNT = "DU1234567"
GATEWAY = "https://localhost:5000/v1/api"
TERMINAL = {"Filled", "Cancelled", "Inactive"}


class FakeIBKR:
    def __init__(self, equity=100000.0):
        self.authenticated = True
        self.ledger = {"BASE": {"netliquidationvalue": equity, "cashbalance": equity, "currency": "BASE"}}
        self.buying_power = equity * 2
        self.orders: list[dict] = []
        self.positions: dict[str, dict] = {}
        self.pending_reply: dict[str, list] = {}
        self.posts: list[dict] = []
        self._next = 1000
        self.conids = {"NVDA": 4815747, "SNDK": 7000001, "COIN": 481691285, "IREN": 7000002, "AAPL": 265598}

    def transport(self):
        return httpx.MockTransport(self.handle)

    # ---- helpers ----
    def by_ref(self, ref):
        return next(o for o in self.orders if o["order_ref"] == ref)

    def _fill(self, o, qty, price):
        prev = o["filledQuantity"]
        avg = float(o["avgPrice"] or 0)
        o["avgPrice"] = str(round((avg * prev + price * qty) / (prev + qty), 4))
        o["filledQuantity"] = prev + qty
        o["remainingQuantity"] = o["totalSize"] - o["filledQuantity"]
        o["status"] = "Filled" if o["remainingQuantity"] <= 0 else "Submitted"
        p = self.positions.setdefault(o["ticker"], {"qty": 0, "avg": 0.0})
        if o["side"] == "BUY":
            p["avg"] = (p["avg"] * p["qty"] + price * qty) / (p["qty"] + qty)
            p["qty"] += qty
        else:
            p["qty"] -= qty
        if p["qty"] == 0:
            del self.positions[o["ticker"]]

    def fill_entry(self, cid, price, qty=None):
        o = self.by_ref(cid)
        self._fill(o, qty or o["remainingQuantity"], price)
        for ch in self.orders:
            if ch["order_ref"] == f"{cid}:stop" and ch["status"] == "PreSubmitted":
                ch["status"] = "Submitted"   # child stop goes live once the parent fills

    def trigger_stop(self, symbol, price):
        for o in self.orders:
            if o["ticker"] == symbol and o["orderType"] == "STP" and o["status"] == "Submitted":
                self._fill(o, o["remainingQuantity"], price)

    def fill_sells(self, symbol, price, tif=None):
        for o in self.orders:
            if o["ticker"] == symbol and o["side"] == "SELL" and o["orderType"] == "MKT" and o["status"] not in TERMINAL:
                self._fill(o, o["remainingQuantity"], price)

    def add_outside_position(self, symbol, qty, avg):
        self.positions[symbol] = {"qty": qty, "avg": avg}

    # ---- placing ----
    def _place(self, tickets):
        out = []
        refs = {t["cOID"]: None for t in tickets}
        for t in tickets:
            if any(o["order_ref"] == t["cOID"] for o in self.orders):
                return {"error": f"Local order ID={t['cOID']} is already registered."}
            sym = next(s for s, c in self.conids.items() if c == t["conid"])
            if t["side"] == "SELL" and "parentId" not in t:
                held = sum(o["remainingQuantity"] for o in self.orders
                           if o["ticker"] == sym and o["side"] == "SELL" and o["status"] == "Submitted")
                avail = self.positions.get(sym, {"qty": 0})["qty"] - held
                if t["quantity"] > avail:
                    return {"error": f"Order rejected: position {avail} insufficient to sell {t['quantity']}"}
            self._next += 1
            o = {"orderId": self._next, "acct": t["acctId"], "conid": t["conid"], "ticker": sym, "side": t["side"],
                 "orderType": t["orderType"], "price": t.get("price"), "timeInForce": t["tif"], "totalSize": t["quantity"],
                 "filledQuantity": 0, "remainingQuantity": t["quantity"], "avgPrice": None, "order_ref": t["cOID"],
                 "status": "PreSubmitted" if t.get("parentId") in refs else "Submitted"}
            self.orders.append(o)
            out.append({"order_id": str(o["orderId"]), "order_status": o["status"], "local_order_id": t["cOID"]})
        return out

    # ---- HTTP ----
    def handle(self, req: httpx.Request) -> httpx.Response:
        path, m = req.url.path.removeprefix("/v1/api"), req.method
        if path == "/iserver/auth/status":
            return httpx.Response(200, json={"authenticated": self.authenticated, "connected": True,
                                             "established": self.authenticated, "competing": False})
        if not self.authenticated:
            return httpx.Response(401, json={"error": "not authenticated"})
        if path == "/portfolio/accounts":
            return httpx.Response(200, json=[{"id": ACCOUNT, "desc": "Paper account"}])
        if path == "/iserver/accounts":
            return httpx.Response(200, json={"accounts": [ACCOUNT], "selectedAccount": ACCOUNT})
        if path == "/iserver/secdef/search":
            sym = req.url.params.get("symbol")
            return httpx.Response(200, json=[{"conid": self.conids[sym], "symbol": sym, "description": "NASDAQ"}] if sym in self.conids else [])
        if path == f"/iserver/account/{ACCOUNT}/orders" and m == "POST":
            body = json.loads(req.content)
            self.posts.append(body)
            rid = f"reply{len(self.posts)}"
            self.pending_reply[rid] = body["orders"]
            return httpx.Response(200, json=[{"id": rid, "message": ["You are submitting an order without market data. Are you sure?"]}])
        if path.startswith("/iserver/reply/") and m == "POST":
            rid = path.rsplit("/", 1)[-1]
            tickets = self.pending_reply.pop(rid, None)
            if tickets is None:
                return httpx.Response(400, json={"error": "unknown reply id"})
            return httpx.Response(200, json=self._place(tickets))
        if path == "/iserver/account/orders":
            return httpx.Response(200, json={"orders": [dict(o) for o in self.orders], "snapshot": True})
        if path.startswith(f"/iserver/account/{ACCOUNT}/order/") and m == "DELETE":
            oid = int(path.rsplit("/", 1)[-1])
            o = next((x for x in self.orders if x["orderId"] == oid), None)
            if not o:
                return httpx.Response(404, json={"error": "order not found"})
            if o["status"] not in TERMINAL:
                o["status"] = "Cancelled"
            return httpx.Response(200, json={"msg": "Request was submitted", "order_id": oid})
        if path == f"/portfolio/{ACCOUNT}/positions/0":
            return httpx.Response(200, json=[{"ticker": s, "contractDesc": s, "position": p["qty"], "avgCost": p["avg"],
                                              "mktValue": p["qty"] * p["avg"]} for s, p in self.positions.items()])
        if path == f"/portfolio/{ACCOUNT}/ledger":
            return httpx.Response(200, json=self.ledger)
        if path == f"/portfolio/{ACCOUNT}/summary":
            return httpx.Response(200, json={"buyingpower": {"amount": self.buying_power, "currency": "USD"}})
        return httpx.Response(404, json={"error": f"unhandled {m} {path}"})
