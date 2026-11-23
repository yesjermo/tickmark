"""Crypto order routing to linked exchange accounts (spot): testnet/demo accounts, and real accounts whose
per-account live switch is on (linked.set_live).

Flow (same shape as the stock ticket):
  preview  -> live ticker, exchange precision and minimums, free balance, and hard checks
              (kill switch, restricted list, per-order size cap, stop below price); stored for 2 minutes
  submit   -> explicit confirm; the preview id is the client order id (UNIQUE locally and at the exchange),
              the row is reserved in a transaction and the exchange is called outside it
  sync     -> fetch_order for working orders; once a buy with a stop has filled, the protective stop is placed
              at the exchange (a stop/trigger sell for the filled amount). If that fails, a critical alert says
              the position is unprotected.
  cancel / close -> cancel at the exchange; close = cancel the stop, then market-sell the filled amount

Real accounts trade only after the user enabled real money for that account with the typed phrase. Real-money
orders also need the base asset typed at submit, and are blocked once the day's realized loss on that account
reaches the daily loss limit. Data comes only from the exchange, never from the stock feed.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from . import alerts, db, events, linked, risk

PREVIEW_TTL = timedelta(minutes=2)
MAX_ORDER_PCT = 25.0            # one order may use at most this % of the account's priced value
OPEN = ("pending", "open", "partially_filled")
STATUS = {"open": "open", "closed": "filled", "canceled": "canceled", "cancelled": "canceled", "expired": "expired",
          "rejected": "rejected"}


class CryptoError(Exception):
    def __init__(self, msg, code=400, payload=None):
        super().__init__(msg)
        self.code, self.payload = code, payload


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ccxt_errors():
    import ccxt
    return ccxt


def _env(acc: dict) -> str:
    return "testnet / demo" if acc.get("sandbox") else "LIVE (real money)"


def tradable_accounts() -> list[dict]:
    out = []
    for a in linked.list_accounts():
        if a["kind"] not in linked.TRADING_KINDS:
            continue
        live = bool(a.get("live_enabled_at")) and not a["sandbox"]
        ok = bool(a["sandbox"]) or live
        out.append({"id": a["id"], "label": a["label"], "provider": a["provider"], "provider_name": a["provider_name"],
                    "sandbox": a["sandbox"], "live": live, "can_trade": ok, "environment": _env(a),
                    "why": "testnet / demo account" if a["sandbox"] else
                           ("real money: live switch on" if live else "real account: read-only until you enable real money for it")})
    return out


def _account(aid: str) -> dict:
    acc = linked.get(aid)
    if linked.PROVIDERS[acc["provider"]]["kind"] not in linked.TRADING_KINDS:
        raise CryptoError("Orders can only go to a linked crypto exchange account.")
    if not acc.get("sandbox") and not acc.get("live_enabled_at"):
        raise CryptoError("This is a real account and real-money trading isn't enabled for it. Enable it in the Broker tab "
                          "(Linked accounts → Real-money trading), or use a testnet / demo account.", 409)
    return acc


def _ex(acc):
    try:
        return linked.exchange(acc)
    except linked.LinkError as exc:
        raise CryptoError(str(exc), exc.code)


def _call(fn, what: str):
    ccxt = _ccxt_errors()
    try:
        return fn()
    except ccxt.InsufficientFunds as exc:
        raise CryptoError(f"Exchange: insufficient funds for {what}. {exc}"[:300], 409)
    except ccxt.InvalidOrder as exc:
        raise CryptoError(f"Exchange refused the {what}: {exc}"[:300], 409)
    except ccxt.AuthenticationError as exc:
        raise CryptoError(f"Exchange rejected the API key: {exc}"[:300], 401)
    except ccxt.NetworkError as exc:
        raise CryptoError(f"Couldn't reach the exchange: {exc}"[:300], 502)
    except ccxt.BaseError as exc:
        raise CryptoError(f"Exchange error on {what}: {exc}"[:300], 502)


def markets(aid: str, q: str = "") -> list[str]:
    acc = _account(aid)
    ex = _ex(acc)
    mk = _call(ex.load_markets, "market list")
    q = q.upper().strip()
    syms = [s for s, m in mk.items() if (m.get("spot", True)) and m.get("active", True) is not False and (not q or q in s)]
    return sorted(syms, key=lambda s: (not s.endswith(("/USDT", "/USD", "/USDC")), s))[:60]


# ---------- preview ----------

def preview(aid: str, symbol: str, side: str, notional: float | None = None, amount: float | None = None,
            order_type: str = "market", limit_price: float | None = None, stop_price: float | None = None) -> dict:
    if db.data_mode() != "live":
        raise CryptoError("Crypto orders need Live data mode (switch at the top).", 409)
    if side not in ("buy", "sell"):
        raise CryptoError("side must be buy or sell")
    if order_type not in ("market", "limit"):
        raise CryptoError("order_type must be market or limit")
    acc = _account(aid)
    ex = _ex(acc)
    mk = _call(ex.load_markets, "market list")
    symbol = symbol.upper().strip()
    m = mk.get(symbol)
    if not m:
        raise CryptoError(f"{symbol} isn't a market on {acc['label']}. Use BASE/QUOTE, e.g. BTC/USDT.", 404)
    base, quote = m.get("base") or symbol.split("/")[0], m.get("quote") or symbol.split("/")[1]
    t = _call(lambda: ex.fetch_ticker(symbol), "price")
    bid, ask, last = t.get("bid"), t.get("ask"), t.get("last")
    ref = (ask if side == "buy" else bid) or last
    if not ref:
        raise CryptoError(f"No current price for {symbol}.", 502)
    px = float(limit_price) if order_type == "limit" and limit_price else float(ref)
    if order_type == "limit" and not limit_price:
        raise CryptoError("A limit order needs a limit price.")
    if amount is None:
        if not notional or notional <= 0:
            raise CryptoError("Enter an amount or a notional size.")
        amount = float(notional) / px
    amt = float(ex.amount_to_precision(symbol, amount))
    if amt <= 0:
        raise CryptoError("Amount rounds to zero at the exchange's precision.")
    cost = amt * px
    bal = _call(ex.fetch_balance, "balance")
    free = bal.get("free") or {}
    snap = (acc.get("snapshot") or {})
    acct_value = snap.get("total_usd") or 0
    lim = (m.get("limits") or {})
    min_amt = (lim.get("amount") or {}).get("min")
    min_cost = (lim.get("cost") or {}).get("min")
    with db.tx() as c:
        ks = risk.kill_switch(c)
        restricted = set(risk.limits(c)["params"].get("restricted", []))
    checks = []

    def chk(cid, name, ok, observed, threshold):
        checks.append({"id": cid, "name": name, "result": "pass" if ok else "block", "observed": observed, "threshold": threshold})

    chk("kill_switch", "Trading kill switch off", not ks.get("on"), "on" if ks.get("on") else "off", "off")
    live = not acc.get("sandbox")
    if live and acct_value:
        with db.tx() as c:
            lim_pct = risk.limits(c)["params"]["daily_loss_limit_pct"]
            today = datetime.now(timezone.utc).date().isoformat()
            from . import pnl
            lost = sum(j["realized_pnl"] or 0 for j in pnl.crypto_journal(c)
                       if j["status"] == "closed" and j["usd_like"] and (j.get("closed_at") or "")[:10] == today and j["venue"] == acc["label"])
        floor = -acct_value * lim_pct / 100
        chk("daily_loss", "Daily loss limit on this account", lost > floor, f"{lost:,.2f} today", f"> {floor:,.2f} ({lim_pct:g}%)")
    chk("restricted", "Asset not on the restricted list", side == "sell" or base not in restricted, base, "not restricted")
    if min_amt:
        chk("min_amount", "Exchange minimum amount", amt >= min_amt, f"{amt:g} {base}", f">= {min_amt:g}")
    if min_cost:
        chk("min_cost", "Exchange minimum order value", cost >= min_cost, f"{cost:,.2f} {quote}", f">= {min_cost:g}")
    if side == "buy":
        have = float(free.get(quote) or 0)
        chk("funds", f"Enough free {quote}", have >= cost * 1.01, f"{have:,.2f}", f">= {cost * 1.01:,.2f} incl. 1% buffer")
        if acct_value:
            pct = cost / acct_value * 100
            chk("size", "Order size vs account value", pct <= MAX_ORDER_PCT, f"{pct:.1f}%", f"<= {MAX_ORDER_PCT:g}%")
        if stop_price is not None:
            chk("stop", "Stop below the entry price", float(stop_price) < px, f"stop {stop_price}", f"< {px:g}")
    else:
        have = float(free.get(base) or 0)
        chk("holdings", f"Enough free {base}", have >= amt, f"{have:g}", f">= {amt:g}")
        if stop_price is not None:
            raise CryptoError("A protective stop is for buys; this is a sell.")
    stop = float(ex.price_to_precision(symbol, stop_price)) if stop_price is not None else None
    loss = round((px - stop) * amt, 2) if stop else None
    pid = "cv" + uuid.uuid4().hex[:16]        # alphanumeric: valid as a client order id on every supported exchange
    pv = {"id": pid, "account_id": aid, "account_label": acc["label"], "provider": acc["provider"],
          "environment": _env(acc), "live": live, "confirm_text": base if live else None, "symbol": symbol, "base": base, "quote": quote, "side": side,
          "order_type": order_type, "amount": amt, "price": px, "limit_price": float(limit_price) if limit_price else None,
          "est_cost": round(cost, 2), "stop_price": stop, "est_loss_if_stopped": loss, "bid": bid, "ask": ask, "last": last,
          "checks": checks, "blocked": any(x["result"] == "block" for x in checks),
          "stop_note": ("After the buy fills, SwingDesk places a stop sell for the filled amount at the exchange."
                        if stop else "No protective stop."),
          "fill_disclaimer": "Crypto trades 24/7 and can move fast; a market order fills at the exchange's price, which can differ from this preview.",
          "expires_at": (datetime.now(timezone.utc) + PREVIEW_TTL).isoformat(timespec="seconds")}
    with db.tx() as c:
        c.execute("INSERT INTO previews(id,payload_json,created_at) VALUES(?,?,?)", (pid, json.dumps(pv), _now()))
    return pv


# ---------- submit ----------

def _row(c, oid):
    r = c.execute("SELECT * FROM crypto_orders WHERE id=?", (oid,)).fetchone()
    return dict(r) if r else None


def _apply(c, oid: str, o: dict) -> None:
    st = STATUS.get(o.get("status") or "", "open")
    filled = float(o.get("filled") or 0)
    if st == "open" and filled > 0:
        st = "partially_filled"
    fee = o.get("fee")
    c.execute("""UPDATE crypto_orders SET exchange_order_id=COALESCE(?, exchange_order_id), status=?, filled=?, average=?, cost=?,
                 fee=?, updated_at=? WHERE id=?""",
              (o.get("id"), st, filled, o.get("average"), o.get("cost"), json.dumps(fee) if fee else None, _now(), oid))


def submit(preview_id: str, confirm: bool, thesis: str = "", live_confirm: str = "") -> dict:
    if not confirm:
        raise CryptoError("Submission requires explicit confirmation.")
    with db.tx() as c:
        if c.execute("SELECT 1 FROM crypto_orders WHERE client_order_id=?", (preview_id,)).fetchone():
            raise CryptoError("Duplicate submission blocked: this preview was already submitted.", 409, {"duplicate": True})
        r = c.execute("SELECT payload_json FROM previews WHERE id=?", (preview_id,)).fetchone()
        if not r or not preview_id.startswith("cv"):
            raise CryptoError("Unknown crypto preview.", 404)
        pv = json.loads(r["payload_json"])
        if datetime.fromisoformat(pv["expires_at"]) < datetime.now(timezone.utc):
            raise CryptoError("Preview expired (crypto prices move fast); open a fresh one.", 410)
        if pv["blocked"]:
            raise CryptoError("This preview failed a hard check; it can't be sent.", 409)
        if risk.kill_switch(c).get("on"):
            raise CryptoError("The trading kill switch is on.", 409)
        if pv.get("live") and (live_confirm or "").strip().upper() != pv["base"].upper():
            raise CryptoError(f"Real money: type {pv['base']} to confirm this order.", 422)
        oid = "co_" + uuid.uuid4().hex[:12]
        c.execute("""INSERT INTO crypto_orders(id,client_order_id,account_id,provider,environment,symbol,side,order_type,purpose,
                     amount,limit_price,stop_price,status,reason,thesis,preview_json,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (oid, preview_id, pv["account_id"], pv["provider"], pv["environment"], pv["symbol"], pv["side"], pv["order_type"],
                   "entry" if pv["side"] == "buy" else "exit", pv["amount"], pv["limit_price"], pv["stop_price"], "pending",
                   f"sending to {pv['account_label']}", thesis or None, json.dumps(pv), _now(), _now()))
    acc = _account(pv["account_id"])                   # re-checked: still a test/demo account
    ex = _ex(acc)
    try:
        o = _call(lambda: ex.create_order(pv["symbol"], pv["order_type"], pv["side"], pv["amount"], pv["limit_price"],
                                          {"clientOrderId": preview_id}), "order")
    except CryptoError as exc:
        with db.tx() as c:
            c.execute("UPDATE crypto_orders SET status='rejected', reason=?, updated_at=? WHERE id=?", (str(exc), _now(), oid))
        raise
    with db.tx() as c:
        _apply(c, oid, o)
        c.execute("UPDATE crypto_orders SET reason=? WHERE id=?", (f"sent to {pv['account_label']}", oid))
    events.bump("crypto order")
    sync()
    return {"order": get_order(oid)}


# ---------- sync, stops, cancel, close ----------

def get_order(oid: str) -> dict:
    c = db.connect()
    try:
        o = _row(c, oid)
        if not o:
            raise CryptoError("Unknown crypto order.", 404)
        o["children"] = [dict(r) for r in c.execute("SELECT * FROM crypto_orders WHERE parent_id=? ORDER BY created_at", (oid,))]
        return o
    finally:
        c.close()


def list_orders(limit: int = 50) -> list[dict]:
    c = db.connect()
    try:
        ids = [r["id"] for r in c.execute("SELECT id FROM crypto_orders WHERE parent_id IS NULL ORDER BY created_at DESC LIMIT ?", (limit,))]
    finally:
        c.close()
    return [get_order(i) for i in ids]


def _place_stop(entry: dict) -> str:
    acc = _account(entry["account_id"])
    ex = _ex(acc)
    sym, amt = entry["symbol"], float(ex.amount_to_precision(entry["symbol"], entry["filled"]))
    cid = f"{entry['client_order_id'][:20]}s1"
    sid = "co_" + uuid.uuid4().hex[:12]
    with db.tx() as c:
        c.execute("""INSERT INTO crypto_orders(id,client_order_id,account_id,provider,environment,symbol,side,order_type,purpose,parent_id,
                     amount,stop_price,status,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (sid, cid, entry["account_id"], entry["provider"], entry["environment"], sym, "sell", "stop", "stop", entry["id"],
                   amt, entry["stop_price"], "pending", "placing protective stop", _now(), _now()))
    try:
        o = _call(lambda: ex.create_stop_order(sym, "market", "sell", amt, None, entry["stop_price"], {"clientOrderId": cid}),
                  "protective stop")
    except CryptoError as exc:
        with db.tx() as c:
            c.execute("UPDATE crypto_orders SET status='rejected', reason=?, updated_at=? WHERE id=?", (str(exc), _now(), sid))
            alerts.emit(c, "crypto", f"Crypto position WITHOUT a stop: {sym}",
                        f"The exchange refused the protective stop at {entry['stop_price']}: {exc}. Place one on the exchange or close the position.",
                        "critical", sym, f"cstop:{entry['id']}")
        alerts.flush()
        return f"{sym}: stop FAILED ({exc})"
    with db.tx() as c:
        _apply(c, sid, o)
        c.execute("UPDATE crypto_orders SET reason='protective stop at the exchange' WHERE id=?", (sid,))
    return f"{sym}: stop placed at {entry['stop_price']}"


def sync() -> dict:
    """Pull status for working crypto orders; place stops for filled buys that have none yet."""
    c = db.connect()
    try:
        rows = [dict(r) for r in c.execute(f"SELECT * FROM crypto_orders WHERE status IN {OPEN} AND exchange_order_id IS NOT NULL")]
    finally:
        c.close()
    errors, notes = [], []
    for r in rows:
        try:
            ex = _ex(_account(r["account_id"]))
            params = {"trigger": True} if r["purpose"] == "stop" else {}
            o = _call(lambda: ex.fetch_order(r["exchange_order_id"], r["symbol"], params), "status check")
            with db.tx() as c:
                _apply(c, r["id"], o)
        except CryptoError as exc:
            errors.append(f"{r['symbol']}: {exc}"[:200])
    # protective stops for filled buys
    c = db.connect()
    try:
        need = [dict(r) for r in c.execute("""SELECT * FROM crypto_orders e WHERE e.purpose='entry' AND e.side='buy' AND e.status='filled'
                   AND e.stop_price IS NOT NULL AND NOT EXISTS (SELECT 1 FROM crypto_orders s WHERE s.parent_id=e.id AND s.purpose='stop')""")]
    finally:
        c.close()
    for e in need:
        try:
            notes.append(_place_stop(e))
        except CryptoError as exc:
            errors.append(str(exc)[:200])
    if rows or need:
        events.bump("crypto sync")
    return {"synced": len(rows), "stops": notes, "errors": errors}


def cancel(oid: str) -> dict:
    o = get_order(oid)
    if o["status"] not in OPEN:
        raise CryptoError(f"Order is {o['status']}; only working orders can be canceled.", 409)
    ex = _ex(_account(o["account_id"]))
    if o["exchange_order_id"]:
        params = {"trigger": True} if o["purpose"] == "stop" else {}
        _call(lambda: ex.cancel_order(o["exchange_order_id"], o["symbol"], params), "cancel")
    with db.tx() as c:
        c.execute("UPDATE crypto_orders SET status='canceled', reason='canceled by user', updated_at=? WHERE id=?", (_now(), oid))
    return get_order(oid)


def close(oid: str) -> dict:
    """Sell the filled amount of a buy at market, canceling its stop first."""
    sync()
    e = get_order(oid)
    if e["purpose"] != "entry" or e["side"] != "buy" or e["filled"] <= 0:
        raise CryptoError("Only a filled buy can be closed.", 409)
    if any(ch["purpose"] == "exit" and ch["status"] in OPEN + ("filled",) for ch in e["children"]):
        raise CryptoError("This position already has an exit.", 409)
    ex = _ex(_account(e["account_id"]))
    for ch in e["children"]:
        if ch["purpose"] == "stop" and ch["status"] in OPEN:
            if ch["exchange_order_id"]:
                _call(lambda: ex.cancel_order(ch["exchange_order_id"], ch["symbol"], {"trigger": True}), "stop cancel")
            with db.tx() as c:
                c.execute("UPDATE crypto_orders SET status='canceled', reason='canceled before close', updated_at=? WHERE id=?", (_now(), ch["id"]))
        elif ch["purpose"] == "stop" and ch["status"] == "filled":
            raise CryptoError("The stop already sold this position.", 409)
    amt = float(ex.amount_to_precision(e["symbol"], e["filled"]))
    cid = f"{e['client_order_id'][:20]}x1"
    xid = "co_" + uuid.uuid4().hex[:12]
    with db.tx() as c:
        c.execute("""INSERT INTO crypto_orders(id,client_order_id,account_id,provider,environment,symbol,side,order_type,purpose,parent_id,
                     amount,status,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (xid, cid, e["account_id"], e["provider"], e["environment"], e["symbol"], "sell", "market", "exit", e["id"],
                   amt, "pending", "close at market", _now(), _now()))
    o = _call(lambda: ex.create_order(e["symbol"], "market", "sell", amt, None, {"clientOrderId": cid}), "close")
    with db.tx() as c:
        _apply(c, xid, o)
    events.bump("crypto close")
    return get_order(oid)
