"""Generic OAuth 2.0 "Connect with <broker>" flow (authorization code + PKCE).

1. /api/oauth/{provider}/start  -> returns the broker's authorize URL (with state + PKCE challenge)
2. user logs in on the broker's site and approves
3. broker redirects to /api/oauth/{provider}/callback?code=..&state=..
4. Tickmark exchanges the code for tokens and stores them in the system keychain
5. access tokens are refreshed automatically; an alert fires before the refresh token
   expires (e.g. Schwab requires logging in again about every 7 days)

A provider is usable once the app is registered with that broker and its client ID
(and secret, where required) is set in the server environment. Without that, the
Connect button explains what is missing. Provider endpoints below come from each
broker's public developer documentation; none has been exercised against the live
providers in this build (tested against a fake provider only).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

from . import credentials, db

TRANSPORT: httpx.BaseTransport | None = None   # injectable for tests
STATE_TTL = 600

PROVIDERS = {
    "schwab": {"name": "Charles Schwab", "authorize_url": "https://api.schwabapi.com/v1/oauth/authorize",
               "token_url": "https://api.schwabapi.com/v1/oauth/token", "scope": "readonly", "client_auth": "basic",
               "client_id_env": "SCHWAB_CLIENT_ID", "client_secret_env": "SCHWAB_CLIENT_SECRET",
               "refresh_ttl_days": 7, "pkce": False,
               "note": "Register an app at developer.schwab.com (approval required). Schwab needs an HTTPS callback URL."},
    "tradier": {"name": "Tradier", "authorize_url": "https://api.tradier.com/v1/oauth/authorize",
                "token_url": "https://api.tradier.com/v1/oauth/accesstoken", "scope": "read write trade", "client_auth": "basic",
                "client_id_env": "TRADIER_CLIENT_ID", "client_secret_env": "TRADIER_CLIENT_SECRET",
                "refresh_ttl_days": None, "pkce": False,
                "note": "OAuth apps are approved by Tradier; individuals can paste a personal access token instead."},
    "alpaca": {"name": "Alpaca", "authorize_url": "https://app.alpaca.markets/oauth/authorize",
               "token_url": "https://api.alpaca.markets/oauth/token", "scope": "account:write trading data", "client_auth": "post",
               "client_id_env": "ALPACA_OAUTH_CLIENT_ID", "client_secret_env": "ALPACA_OAUTH_CLIENT_SECRET",
               "refresh_ttl_days": None, "pkce": False,
               "note": "Register an OAuth app with Alpaca Connect to offer 'Sign in with Alpaca'. Until then, users paste API keys."},
    "tradestation": {"name": "TradeStation", "authorize_url": "https://signin.tradestation.com/authorize",
                     "token_url": "https://signin.tradestation.com/oauth/token",
                     "scope": "openid offline_access profile MarketData ReadAccount Trade", "client_auth": "post",
                     "client_id_env": "TRADESTATION_CLIENT_ID", "client_secret_env": "TRADESTATION_CLIENT_SECRET",
                     "refresh_ttl_days": None, "pkce": True, "extra": {"audience": "https://api.tradestation.com"},
                     "note": "Request an API key from TradeStation."},
}


def _secret_name(provider: str) -> str:
    return f"oauth_{provider}_tokens"


def redirect_uri(provider: str) -> str:
    base = (os.environ.get("SWINGDESK_OAUTH_REDIRECT_BASE") or os.environ.get("SWINGDESK_PUBLIC_URL")
            or os.environ.get("RENDER_EXTERNAL_URL") or "http://127.0.0.1:8000").rstrip("/")
    return f"{base}/api/oauth/{provider}/callback"


def client(provider: str) -> tuple[str | None, str | None]:
    p = PROVIDERS[provider]
    return os.environ.get(p["client_id_env"]), os.environ.get(p["client_secret_env"])


def tokens(provider: str) -> dict | None:
    raw = credentials.get_secret(_secret_name(provider))
    return json.loads(raw) if raw else None


def _save_tokens(provider: str, data: dict, prev: dict | None = None) -> dict:
    now = int(time.time())
    p = PROVIDERS[provider]
    t = {"access_token": data["access_token"],
         "refresh_token": data.get("refresh_token") or (prev or {}).get("refresh_token"),
         "expires_at": now + int(data.get("expires_in", 1800)),
         "obtained_at": now,
         "refresh_expires_at": (prev or {}).get("refresh_expires_at") if not data.get("refresh_token") else
                               (now + p["refresh_ttl_days"] * 86400 if p["refresh_ttl_days"] else None),
         "scope": data.get("scope")}
    credentials.set_secret(_secret_name(provider), json.dumps(t))
    return t


def status(provider: str) -> dict:
    p = PROVIDERS[provider]
    cid, _ = client(provider)
    t = tokens(provider)
    now = int(time.time())
    out = {"id": provider, "name": p["name"], "configured": bool(cid), "connected": bool(t), "note": p["note"],
           "redirect_uri": redirect_uri(provider), "client_id_env": p["client_id_env"]}
    if t:
        out.update(access_expires_in=t["expires_at"] - now,
                   relogin_in_days=None if not t.get("refresh_expires_at") else round((t["refresh_expires_at"] - now) / 86400, 1))
    return out


def start(provider: str) -> dict:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider}")
    p = PROVIDERS[provider]
    cid, _ = client(provider)
    if not cid:
        raise ValueError(f"{p['name']} isn't set up on this server yet: set {p['client_id_env']} "
                         f"(and {p['client_secret_env']}) after registering the app. {p['note']}")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    pending = db.get_setting("oauth_pending", {}) or {}
    now = int(time.time())
    pending = {k: v for k, v in pending.items() if v["at"] > now - STATE_TTL}
    pending[state] = {"provider": provider, "verifier": verifier, "at": now}
    db.set_setting("oauth_pending", pending)
    q = {"response_type": "code", "client_id": cid, "redirect_uri": redirect_uri(provider), "state": state,
         "scope": p["scope"], **p.get("extra", {})}
    if p["pkce"]:
        q["code_challenge"] = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        q["code_challenge_method"] = "S256"
    return {"authorize_url": f"{p['authorize_url']}?{urlencode(q)}", "state": state}


def _token_request(provider: str, form: dict) -> dict:
    p = PROVIDERS[provider]
    cid, csecret = client(provider)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if p["client_auth"] == "basic":
        headers["Authorization"] = "Basic " + base64.b64encode(f"{cid}:{csecret or ''}".encode()).decode()
    else:
        form = {**form, "client_id": cid, **({"client_secret": csecret} if csecret else {})}
    with httpx.Client(transport=TRANSPORT, timeout=15) as c:
        r = c.post(p["token_url"], data=form, headers=headers)
    if r.status_code >= 400:
        raise ValueError(f"{p['name']} token request failed ({r.status_code}): {r.text[:200]}")
    return r.json()


def callback(provider: str, code: str, state: str) -> dict:
    pending = db.get_setting("oauth_pending", {}) or {}
    entry = pending.pop(state, None)
    db.set_setting("oauth_pending", pending)
    if not entry or entry["provider"] != provider or entry["at"] < int(time.time()) - STATE_TTL:
        raise ValueError("This sign-in link is invalid or expired. Start again from Tickmark.")
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(provider)}
    if PROVIDERS[provider]["pkce"]:
        form["code_verifier"] = entry["verifier"]
    return _save_tokens(provider, _token_request(provider, form))


def access_token(provider: str) -> str:
    """A valid access token, refreshing it first if it expires within a minute."""
    t = tokens(provider)
    if not t:
        raise ValueError(f"{PROVIDERS[provider]['name']} is not connected")
    if t["expires_at"] - 60 > int(time.time()):
        return t["access_token"]
    if not t.get("refresh_token"):
        raise ValueError("Session expired; connect again")
    if t.get("refresh_expires_at") and t["refresh_expires_at"] < int(time.time()):
        raise ValueError(f"{PROVIDERS[provider]['name']} requires logging in again (refresh token expired)")
    return _save_tokens(provider, _token_request(provider, {"grant_type": "refresh_token", "refresh_token": t["refresh_token"]}), t)["access_token"]


def disconnect(provider: str) -> None:
    credentials.set_secret(_secret_name(provider), None)


def expiry_alerts(conn) -> int:
    """Emit a warning when a connection must be re-authorised within 24 hours."""
    from . import alerts
    n = 0
    for pid in PROVIDERS:
        st = status(pid)
        if st["connected"] and st.get("relogin_in_days") is not None and st["relogin_in_days"] <= 1:
            alerts.emit(conn, "broker", f"{st['name']}: log in again within a day",
                        f"{st['name']} requires re-authorising about every {PROVIDERS[pid]['refresh_ttl_days']} days. "
                        "Open the Broker tab and click Connect.", "warn", None, f"relogin:{pid}:{int(time.time()) // 86400}")
            n += 1
    return n
