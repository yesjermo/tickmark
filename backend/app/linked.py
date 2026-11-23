"""Linked accounts: read-only views of other brokers, exchanges and wallets.

The trading broker (Broker tab: simulator, Alpaca, IBKR) is where Tickmark sends orders.
Linked accounts are different: Tickmark only *reads* balances and positions from them, so the
journal, risk view and copilot can see the whole picture. No order code exists here.

Providers
  ccxt          Coinbase, Binance, Binance.US, Kraken, OKX, Bybit through the ccxt library.
                Testnet / demo by default where the exchange offers one (Binance, Binance.US, OKX, Bybit);
                Coinbase and Kraken have no spot sandbox, so they read the real account; use a key
                with view / read-only permission.
  robinhood     Robinhood Crypto Trading API (official; Ed25519-signed requests). Robinhood has no
                public API for stocks; those come later through an aggregator (SnapTrade).
  futu          moomoo / Futu through the local OpenD gateway and the official futu-api (or moomoo-api)
                SDK. Paper ("simulate") environment by default.
  solana        Phantom (or any Solana wallet), watch-only: the public address is enough. Never a
                seed phrase or private key.

Secrets live in the OS keychain (credentials.set_secret); non-secret fields in settings.json.
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime, timezone

from . import credentials, db

CCXT_FACTORY = None          # tests: lambda exchange_id, config -> fake exchange
ROBINHOOD_TRANSPORT = None   # tests: httpx transport
SOLANA_TRANSPORT = None      # tests: httpx transport
FUTU_MODULE = None           # tests: fake SDK module

STABLE = {"USD", "USDT", "USDC", "FDUSD", "DAI", "PYUSD", "USDG"}
SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
KNOWN_MINTS = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}

F = lambda key, label, secret=False, default=None, placeholder=None: {  # noqa: E731
    "key": key, "label": label, "secret": secret, "default": default, "placeholder": placeholder}

PROVIDERS = {
    "coinbase": {"kind": "ccxt", "ccxt": "coinbase", "name": "Coinbase (Advanced Trade)", "sandbox": False,
                 "fields": [F("apiKey", "API key name", placeholder="organizations/…/apiKeys/…"),
                            F("secret", "API private key (PEM)", True)],
                 "help": "Coinbase Developer Platform → API keys → create a key with View permission only. Coinbase has no spot sandbox, so this reads your real account."},
    "binance": {"kind": "ccxt", "ccxt": "binance", "name": "Binance", "sandbox": True,
                "fields": [F("apiKey", "API key"), F("secret", "Secret key", True)],
                "help": "Testnet by default: create keys at testnet.binance.vision. For the real account, use a key with only 'Enable Reading'. Binance.com (and its testnet) refuse US connections; US residents use Binance.US.",
                "geo_hint": "Binance.com and its testnet refuse US connections (checked 2026-11-22). US residents: link Binance.US instead."},
    "binanceus": {"kind": "ccxt", "ccxt": "binanceus", "name": "Binance.US", "sandbox": True,
                  "fields": [F("apiKey", "API key"), F("secret", "Secret key", True)],
                  "help": "For US residents. Use a read-only key for the real account."},
    "kraken": {"kind": "ccxt", "ccxt": "kraken", "name": "Kraken", "sandbox": False,
               "fields": [F("apiKey", "API key"), F("secret", "Private key", True)],
               "help": "Settings → API → create a key with only 'Query Funds' (and 'Query Open Orders & Trades'). Kraken has no spot sandbox."},
    "okx": {"kind": "ccxt", "ccxt": "okx", "name": "OKX", "sandbox": True,
            "fields": [F("apiKey", "API key"), F("secret", "Secret key", True), F("password", "Passphrase", True)],
            "help": "Demo trading by default: create the key under Trade → Demo trading → API. Real account: 'Read' permission only."},
    "bybit": {"kind": "ccxt", "ccxt": "bybit", "name": "Bybit", "sandbox": True,
              "fields": [F("apiKey", "API key"), F("secret", "API secret", True)],
              "help": "Testnet by default: create keys at testnet.bybit.com. Real account: 'Read-Only' key. Bybit (and its testnet) refuse US connections.",
              "geo_hint": "Bybit and its testnet refuse US connections (checked 2026-11-22)."},
    "robinhood_crypto": {"kind": "robinhood", "name": "Robinhood Crypto", "sandbox": False,
                         "fields": [F("api_key", "API key", placeholder="rh-api-…"), F("private_key", "Private key (base64)", True)],
                         "help": "Robinhood app (web) → Settings → Crypto → API trading: generate an Ed25519 key pair, register the public key, paste the API key and your base64 private key here. Robinhood stocks have no public API."},
    "futu": {"kind": "futu", "name": "moomoo / Futu (OpenD)", "sandbox": True,
             "fields": [F("host", "OpenD host", default="127.0.0.1"), F("port", "OpenD port", default="11111"),
                        F("firm", "Broker entity", default="FUTUINC", placeholder="FUTUINC (moomoo US), FUTUSECURITIES (HK), FUTUSG, FUTUAU…"),
                        F("market", "Market", default="US", placeholder="US or HK")],
             "help": "Install and run moomoo/Futu OpenD and log in there; Tickmark connects to it locally. Paper (simulate) account by default. Needs: pip install futu-api (or moomoo-api)."},
    "phantom": {"kind": "solana", "name": "Phantom wallet (Solana, watch-only)", "sandbox": False,
                "fields": [F("address", "Public wallet address"), F("rpc_url", "Solana RPC URL", default="https://api.mainnet-beta.solana.com")],
                "help": "Paste only the PUBLIC address (Phantom → Receive → copy). Never enter a seed phrase or private key anywhere. The address is sent to the Solana RPC you choose."},
}


class LinkError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def providers() -> list[dict]:
    return [{"id": k, **{x: v[x] for x in ("kind", "name", "sandbox", "fields", "help")}} for k, v in PROVIDERS.items()]


# ---------- storage ----------

def _all() -> list[dict]:
    return db.get_setting("linked_accounts") or []


def _put(rows: list[dict]) -> None:
    db.set_setting("linked_accounts", rows)


def _secret_name(aid: str, key: str) -> str:
    return f"linked:{aid}:{key}"


def _config(acc: dict) -> dict:
    prov = PROVIDERS[acc["provider"]]
    cfg = dict(acc.get("fields") or {})
    for f in prov["fields"]:
        if f["secret"]:
            cfg[f["key"]] = credentials.get_secret(_secret_name(acc["id"], f["key"]))
    return cfg


def public(acc: dict) -> dict:
    return {k: acc.get(k) for k in ("id", "provider", "label", "sandbox", "fields", "created_at", "snapshot", "error", "live_enabled_at")} | \
        {"provider_name": PROVIDERS[acc["provider"]]["name"], "kind": PROVIDERS[acc["provider"]]["kind"]}


def list_accounts() -> list[dict]:
    return [public(a) for a in _all() if a.get("provider") in PROVIDERS]


def add(provider: str, label: str, sandbox: bool, values: dict) -> dict:
    prov = PROVIDERS.get(provider)
    if not prov:
        raise LinkError(f"Unknown provider '{provider}'.")
    values = {k: (str(v).strip() if v is not None else "") for k, v in (values or {}).items()}
    fields, secrets = {}, {}
    for f in prov["fields"]:
        v = values.get(f["key"]) or (f["default"] or "")
        if not v:
            raise LinkError(f"{f['label']} is required.")
        (secrets if f["secret"] else fields)[f["key"]] = v
    if provider == "phantom":
        _check_solana_address(fields["address"])
    aid = "la_" + uuid.uuid4().hex[:10]
    acc = {"id": aid, "provider": provider, "label": label.strip() or prov["name"], "sandbox": bool(sandbox and prov["sandbox"]),
           "fields": fields, "created_at": _now()}
    snap = fetch(acc, {**fields, **secrets})          # test before saving anything
    for k, v in secrets.items():
        credentials.set_secret(_secret_name(aid, k), v)
    acc["snapshot"], acc["error"] = snap, None
    _put([*_all(), acc])
    return public(acc)


def refresh(aid: str) -> dict:
    rows = _all()
    acc = next((a for a in rows if a["id"] == aid), None)
    if not acc:
        raise LinkError("Unknown linked account.", 404)
    try:
        acc["snapshot"], acc["error"] = fetch(acc, _config(acc)), None
    except LinkError as exc:
        acc["error"] = str(exc)
    _put(rows)
    return public(acc)


def remove(aid: str) -> dict:
    rows = _all()
    acc = next((a for a in rows if a["id"] == aid), None)
    if not acc:
        raise LinkError("Unknown linked account.", 404)
    for f in PROVIDERS[acc["provider"]]["fields"]:
        if f["secret"]:
            credentials.set_secret(_secret_name(aid, f["key"]), None)
    _put([a for a in rows if a["id"] != aid])
    return {"removed": aid}


def summary() -> dict:
    """Totals across linked accounts (for the copilot and the Broker tab)."""
    accs = list_accounts()
    total = sum((a.get("snapshot") or {}).get("total_usd") or 0 for a in accs)
    return {"accounts": accs, "total_usd_valued": round(total, 2),
            "note": "Read-only snapshots; total counts only holdings that could be priced in USD."}


# ---------- fetchers ----------

def fetch(acc: dict, cfg: dict) -> dict:
    kind = PROVIDERS[acc["provider"]]["kind"]
    fn = {"ccxt": _ccxt, "robinhood": _robinhood, "futu": _futu, "solana": _solana}[kind]
    snap = fn(acc, cfg)
    snap["as_of"] = _now()
    snap["total_usd"] = round(sum(h.get("usd_value") or 0 for h in snap["holdings"]), 2)
    snap["environment"] = "testnet / paper" if acc.get("sandbox") else "real account (read-only)"
    return snap


TRADING_KINDS = ("ccxt", "robinhood")
LIVE_PHRASE = "I understand this trades real money"


def live_status(acc: dict) -> dict:
    """What enabling real-money trading on this linked account would need, and whether it's on."""
    from . import risk
    prov = PROVIDERS[acc["provider"]]
    c = db.connect()
    try:
        db.init_schema(c)
        lim_v = risk.limits(c)["version"]
        ks = risk.kill_switch(c).get("on")
    finally:
        c.close()
    checks = [
        {"name": "Venue supports orders", "ok": prov["kind"] in TRADING_KINDS, "detail": prov["kind"]},
        {"name": "Linked as a real account (not testnet)", "ok": not acc.get("sandbox"), "detail": "testnet / demo" if acc.get("sandbox") else "real"},
        {"name": "Live data mode", "ok": db.data_mode() == "live", "detail": db.data_mode()},
        {"name": "Risk limits deliberately set", "ok": lim_v >= 1, "detail": f"limits v{lim_v}"},
        {"name": "Kill switch off", "ok": not ks, "detail": "on" if ks else "off"},
    ]
    return {"eligible": all(x["ok"] for x in checks), "checks": checks, "enabled_at": acc.get("live_enabled_at"), "phrase": LIVE_PHRASE}


def set_live(aid: str, on: bool, phrase: str = "") -> dict:
    rows = _all()
    acc = next((a for a in rows if a["id"] == aid), None)
    if not acc:
        raise LinkError("Unknown linked account.", 404)
    if on:
        st = live_status(acc)
        if not st["eligible"]:
            raise LinkError("Can't enable real-money trading here yet: " + "; ".join(c["name"] for c in st["checks"] if not c["ok"]), 409)
        if (phrase or "").strip() != LIVE_PHRASE:
            raise LinkError(f'Type exactly: "{LIVE_PHRASE}"', 422)
        acc["live_enabled_at"] = _now()
        with db.tx() as c:
            c.execute("INSERT INTO risk_overrides(client_order_id,symbol,check_id,observed,threshold,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                      ("-", acc["label"], "crypto_live_enabled", acc["provider"], "per-account live switch", phrase, _now()))
    else:
        acc["live_enabled_at"] = None
    _put(rows)
    return public(acc)


def exchange(acc: dict, cfg: dict | None = None):
    """An order-capable client for a linked account: ccxt for exchanges, the Robinhood Crypto wrapper for Robinhood.
    Testnet/demo when the account was linked that way."""
    prov = PROVIDERS[acc["provider"]]
    cfg = cfg if cfg is not None else _config(acc)
    if prov["kind"] == "robinhood":
        from .brokers.robinhood_crypto import RobinhoodCrypto
        if CCXT_FACTORY:
            return CCXT_FACTORY("robinhood", {"apiKey": cfg.get("api_key"), "secret": cfg.get("private_key")})
        return RobinhoodCrypto(cfg["api_key"], cfg["private_key"])
    conf = {"apiKey": cfg.get("apiKey"), "secret": cfg.get("secret"), "enableRateLimit": True, "timeout": 15000}
    if cfg.get("password"):
        conf["password"] = cfg["password"]
    if CCXT_FACTORY:
        ex = CCXT_FACTORY(prov["ccxt"], conf)
    else:
        import ccxt
        ex = getattr(ccxt, prov["ccxt"])(conf)
    if acc.get("sandbox"):
        ex.set_sandbox_mode(True)
    return ex


def get(aid: str) -> dict:
    acc = next((a for a in _all() if a["id"] == aid), None)
    if not acc:
        raise LinkError("Unknown linked account.", 404)
    return acc


def _ccxt(acc: dict, cfg: dict) -> dict:
    prov = PROVIDERS[acc["provider"]]
    ex = exchange(acc, cfg)
    import ccxt as _c
    try:
        bal = ex.fetch_balance()
    except _c.AuthenticationError as exc:
        raise LinkError(f"{prov['name']} rejected the key: {exc}"[:300])
    except _c.PermissionDenied as exc:
        raise LinkError(f"{prov['name']} denied access (check the key's permissions or IP allow-list): {exc}"[:300])
    except (_c.NetworkError, _c.ExchangeError) as exc:
        txt = str(exc)
        if " 451 " in txt or (" 403 " in txt and "Forbidden" in txt):
            raise LinkError(f"{prov['name']} blocks connections from your location (HTTP {'451' if ' 451 ' in txt else '403'}). "
                            + prov.get("geo_hint", "Use an exchange that serves your region."), 451)
        if isinstance(exc, _c.NetworkError):
            raise LinkError(f"Couldn't reach {prov['name']}: {exc}"[:300], 502)
        raise LinkError(f"{prov['name']} error: {exc}"[:300], 502)
    except _c.BaseError as exc:
        raise LinkError(f"{prov['name']} error: {exc}"[:300], 502)
    totals = {a: float(v) for a, v in (bal.get("total") or {}).items() if v}
    free = bal.get("free") or {}
    try:
        markets = ex.load_markets()
    except Exception:
        markets = {}
    holdings = []
    for asset, qty in sorted(totals.items(), key=lambda kv: -kv[1])[:40]:
        usd, price, note = None, None, None
        if asset in STABLE:
            price = 1.0
        else:
            for quote in ("USD", "USDT", "USDC"):
                sym = f"{asset}/{quote}"
                if sym in markets:
                    try:
                        t = ex.fetch_ticker(sym)
                        price = t.get("last") or t.get("close")
                        note = f"priced on {sym}"
                    except Exception:
                        pass
                    break
        if price:
            usd = round(qty * float(price), 2)
        holdings.append({"asset": asset, "quantity": qty, "available": float(free.get(asset) or 0), "price_usd": price,
                         "usd_value": usd, "note": note})
    return {"holdings": holdings}


def _robinhood(acc: dict, cfg: dict) -> dict:
    import httpx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    try:
        seed = base64.b64decode(cfg["private_key"])
        key = Ed25519PrivateKey.from_private_bytes(seed[:32])
    except Exception:
        raise LinkError("The private key must be the base64 Ed25519 private key you generated for Robinhood.")

    def get(c, path):
        ts = str(int(time.time()))
        msg = f"{cfg['api_key']}{ts}{path}GET".encode()
        sig = base64.b64encode(key.sign(msg)).decode()
        r = c.get(path, headers={"x-api-key": cfg["api_key"], "x-timestamp": ts, "x-signature": sig})
        if r.status_code in (401, 403):
            raise LinkError("Robinhood rejected the key or signature.")
        if r.status_code >= 400:
            raise LinkError(f"Robinhood error {r.status_code}: {r.text[:200]}", 502)
        return r.json()

    try:
        with httpx.Client(base_url="https://trading.robinhood.com", transport=ROBINHOOD_TRANSPORT, timeout=15) as c:
            a = get(c, "/api/v1/crypto/trading/accounts/")
            rows, path = [], "/api/v1/crypto/trading/holdings/"
            while path:
                h = get(c, path)
                rows += h.get("results", [])
                nxt = h.get("next")
                path = nxt.replace("https://trading.robinhood.com", "") if nxt else None
            holdings = []
            for r in rows:
                q = float(r.get("total_quantity") or 0)
                if not q:
                    continue
                sym = f"{r['asset_code']}-USD"
                price = None
                try:
                    bb = get(c, f"/api/v1/crypto/marketdata/best_bid_ask/?symbol={sym}")
                    res = (bb.get("results") or [{}])[0]
                    price = float(res.get("price") or 0) or None
                except LinkError:
                    pass
                holdings.append({"asset": r["asset_code"], "quantity": q, "available": float(r.get("quantity_available_for_trading") or 0),
                                 "price_usd": price, "usd_value": round(q * price, 2) if price else None, "note": None})
    except httpx.HTTPError as exc:
        raise LinkError(f"Couldn't reach Robinhood: {exc}"[:200], 502)
    bp = float(a.get("buying_power") or 0)
    if bp:
        holdings.insert(0, {"asset": a.get("buying_power_currency", "USD"), "quantity": bp, "available": bp, "price_usd": 1.0,
                            "usd_value": round(bp, 2), "note": "buying power"})
    return {"holdings": holdings, "account": {"number_hint": str(a.get("account_number", ""))[-4:], "status": a.get("status")}}


def _futu(acc: dict, cfg: dict) -> dict:
    mod = FUTU_MODULE
    if mod is None:
        try:
            import futu as mod  # type: ignore
        except ImportError:
            try:
                import moomoo as mod  # type: ignore
            except ImportError:
                raise LinkError("The moomoo/Futu SDK isn't installed. Run: pip install futu-api (or moomoo-api), then start OpenD.")
    firm = getattr(mod.SecurityFirm, cfg.get("firm", "FUTUINC"), None)
    market = getattr(mod.TrdMarket, cfg.get("market", "US"), None)
    if firm is None or market is None:
        raise LinkError("Unknown broker entity or market for moomoo/Futu.")
    env = mod.TrdEnv.SIMULATE if acc.get("sandbox") else mod.TrdEnv.REAL
    try:
        ctx = mod.OpenSecTradeContext(filter_trdmarket=market, host=cfg.get("host", "127.0.0.1"), port=int(cfg.get("port", 11111)),
                                      security_firm=firm)
    except Exception as exc:
        raise LinkError(f"Couldn't connect to OpenD at {cfg.get('host')}:{cfg.get('port')}: {exc}"[:200], 502)
    try:
        ret, info = ctx.accinfo_query(trd_env=env)
        if ret != mod.RET_OK:
            raise LinkError(f"OpenD: {info}"[:300], 502)
        ret, pos = ctx.position_list_query(trd_env=env)
        if ret != mod.RET_OK:
            raise LinkError(f"OpenD: {pos}"[:300], 502)
    finally:
        ctx.close()
    first = info.iloc[0] if hasattr(info, "iloc") else info[0]
    cash = float(first["cash"])
    holdings = [{"asset": "cash", "quantity": cash, "available": cash, "price_usd": 1.0, "usd_value": round(cash, 2), "note": cfg.get("market")}]
    records = pos.to_dict("records") if hasattr(pos, "to_dict") else list(pos)
    for p in records:
        q = float(p.get("qty") or 0)
        if q:
            mv = float(p.get("market_val") or 0)
            holdings.append({"asset": p.get("code"), "quantity": q, "available": float(p.get("can_sell_qty") or q),
                             "price_usd": float(p.get("nominal_price") or 0) or None, "usd_value": round(mv, 2) if mv else None,
                             "note": f"cost {p.get('cost_price')}"})
    return {"holdings": holdings, "account": {"total_assets": float(first["total_assets"]), "buying_power": float(first.get("power", 0) or 0)}}


_B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _check_solana_address(a: str) -> None:
    if not (32 <= len(a) <= 44 and set(a) <= _B58):
        raise LinkError("That doesn't look like a Solana public address (32–44 base58 characters). "
                        "Never paste a seed phrase or private key.")


def _solana(acc: dict, cfg: dict) -> dict:
    import httpx
    addr, url = cfg["address"], cfg.get("rpc_url") or "https://api.mainnet-beta.solana.com"

    def rpc(c, method, params):
        r = c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        d = r.json()
        if "error" in d:
            raise LinkError(f"Solana RPC: {d['error'].get('message')}"[:200], 502)
        return d["result"]
    try:
        with httpx.Client(transport=SOLANA_TRANSPORT, timeout=15) as c:
            lamports = rpc(c, "getBalance", [addr])["value"]
            toks = rpc(c, "getTokenAccountsByOwner", [addr, {"programId": SPL_TOKEN}, {"encoding": "jsonParsed"}])["value"]
    except httpx.HTTPError as exc:
        raise LinkError(f"Couldn't reach the Solana RPC: {exc}"[:200], 502)
    holdings = [{"asset": "SOL", "quantity": lamports / 1e9, "available": lamports / 1e9, "price_usd": None, "usd_value": None,
                 "note": "not priced (no price feed)"}]
    for t in toks:
        info = t["account"]["data"]["parsed"]["info"]
        q = float(info["tokenAmount"].get("uiAmount") or 0)
        if q:
            sym = KNOWN_MINTS.get(info["mint"])
            holdings.append({"asset": sym or info["mint"][:6] + "…", "quantity": q, "available": q,
                             "price_usd": 1.0 if sym in STABLE else None, "usd_value": round(q, 2) if sym in STABLE else None,
                             "note": None if sym else f"mint {info['mint']}"})
    return {"holdings": holdings, "account": {"address_hint": addr[:4] + "…" + addr[-4:]}}
