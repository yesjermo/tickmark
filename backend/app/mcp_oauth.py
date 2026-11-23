"""OAuth 2.1 for MCP clients: lets people use SwingDesk from inside Claude (or another MCP app) on their own AI plan.

Flow (MCP authorization spec): the AI app finds the authorization server through
/.well-known/oauth-protected-resource, registers itself (dynamic client registration), sends the user to
/oauth/authorize, where they sign in to SwingDesk and approve, then swaps the code (PKCE S256 required) for an
access token (1 hour) and a rotating refresh token (30 days). /mcp accepts these tokens like personal tokens, and
the same guardrails apply: read the desk and draft proposals, never trade.

Public clients only (no client secrets); redirect URIs must be https, or http on localhost.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from . import portal

ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
CODE_TTL = timedelta(minutes=10)
SCOPE = "swingdesk"


class OAuthError(Exception):
    def __init__(self, error: str, desc: str, code: int = 400):
        super().__init__(desc)
        self.error, self.desc, self.code = error, desc, code


def _now():
    return datetime.now(timezone.utc)


def _iso(d):
    return d.isoformat(timespec="seconds")


def _h(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def conn():
    c = portal.conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, name TEXT, redirect_uris TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS oauth_requests (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, params TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS oauth_codes (code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, user_id TEXT NOT NULL, redirect_uri TEXT NOT NULL,
      challenge TEXT NOT NULL, resource TEXT, expires_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS oauth_tokens (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, user_id TEXT NOT NULL, access_hash TEXT NOT NULL UNIQUE,
      refresh_hash TEXT NOT NULL UNIQUE, access_expires TEXT NOT NULL, refresh_expires TEXT NOT NULL, created_at TEXT NOT NULL,
      last_used_at TEXT, revoked_at TEXT);
    """)
    return c


def base_url(request_base: str) -> str:
    return (os.environ.get("SWINGDESK_PUBLIC_URL") or request_base).rstrip("/")


def resource_metadata(base: str) -> dict:
    return {"resource": f"{base}/mcp", "authorization_servers": [base], "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"], "resource_name": "SwingDesk"}


def server_metadata(base: str) -> dict:
    return {"issuer": base, "authorization_endpoint": f"{base}/oauth/authorize", "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register", "revocation_endpoint": f"{base}/oauth/revoke",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE]}


def _redirect_ok(u: str) -> bool:
    p = urlparse(u)
    if p.fragment:
        return False
    if p.scheme == "https" and p.hostname:
        return True
    return p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1")


def register(body: dict) -> dict:
    uris = body.get("redirect_uris") or []
    if not uris or not all(isinstance(u, str) and _redirect_ok(u) for u in uris):
        raise OAuthError("invalid_redirect_uri", "redirect_uris must be https URLs (or http on localhost), without fragments")
    if body.get("token_endpoint_auth_method", "none") != "none":
        raise OAuthError("invalid_client_metadata", "only public clients (token_endpoint_auth_method=none) are supported")
    cid = "mcpc_" + uuid.uuid4().hex[:20]
    name = str(body.get("client_name") or "AI app")[:80]
    c = conn()
    try:
        c.execute("INSERT INTO oauth_clients(client_id,name,redirect_uris,created_at) VALUES(?,?,?,?)", (cid, name, json.dumps(uris), _iso(_now())))
    finally:
        c.close()
    return {"client_id": cid, "client_name": name, "redirect_uris": uris, "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none", "client_id_issued_at": int(_now().timestamp())}


def _client(c, cid):
    r = c.execute("SELECT * FROM oauth_clients WHERE client_id=?", (cid,)).fetchone()
    if not r:
        raise OAuthError("invalid_client", "unknown client_id", 401)
    return r


def validate_authorize(q: dict) -> dict:
    """Checks the request before showing the consent screen. Errors here are shown, never redirected (unknown target)."""
    c = conn()
    try:
        cl = _client(c, q.get("client_id", ""))
    finally:
        c.close()
    if q.get("redirect_uri") not in json.loads(cl["redirect_uris"]):
        raise OAuthError("invalid_request", "redirect_uri isn't registered for this client")
    if q.get("response_type") != "code":
        raise OAuthError("unsupported_response_type", "response_type must be code")
    if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
        raise OAuthError("invalid_request", "PKCE with code_challenge_method=S256 is required")
    return {"client_id": cl["client_id"], "client_name": cl["name"], "redirect_uri": q["redirect_uri"],
            "code_challenge": q["code_challenge"], "state": q.get("state", ""), "resource": q.get("resource", "")}


def start_consent(user_id: str, params: dict) -> str:
    rid = secrets.token_urlsafe(18)
    c = conn()
    try:
        c.execute("INSERT INTO oauth_requests(id,user_id,params,created_at) VALUES(?,?,?,?)", (rid, user_id, json.dumps(params), _iso(_now())))
    finally:
        c.close()
    return rid


def consent_page(rid: str, params: dict, who: str) -> str:
    e = html.escape
    host = urlparse(params["redirect_uri"]).hostname or ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect to SwingDesk</title><style>
:root{{color-scheme:dark}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0e1115;color:#e4e7eb;font:15px/1.5 system-ui,sans-serif;padding:16px}}
.card{{max-width:440px;width:100%;background:#151920;border:1px solid #272e38;border-radius:10px;padding:24px}}
h1{{font-size:19px;margin:0 0 6px}}ul{{padding-left:18px;color:#c9ced6}}.muted{{color:#8a93a2;font-size:13px}}
.row{{display:flex;gap:10px;margin-top:18px}}button{{flex:1;padding:10px;border-radius:7px;border:1px solid #272e38;font:inherit;cursor:pointer}}
.ok{{background:#5ea8e8;color:#07121d;border-color:#5ea8e8;font-weight:600}}.no{{background:none;color:#e4e7eb}}</style></head>
<body><form class="card" method="post" action="/oauth/authorize"><input type="hidden" name="request_id" value="{e(rid)}">
<h1>Connect {e(params['client_name'])} to SwingDesk?</h1>
<p class="muted">Signed in as {e(who)}. It will return to {e(host)}.</p>
<p>It will be able to:</p><ul><li>read your desk: watchlist screen, rule evidence, indicators, positions, journal, risk limits</li>
<li>draft proposals (rule changes, orders, exits) that wait for you in SwingDesk</li></ul>
<p>It <b>can't</b> place, cancel or close orders, change limits or keys, or move money.</p>
<p class="muted">Its AI usage is billed to your plan with that app, not to SwingDesk. Revoke access any time in SwingDesk → Account.</p>
<div class="row"><button class="no" name="decision" value="deny">Cancel</button><button class="ok" name="decision" value="approve">Allow</button></div>
</form></body></html>"""


def decide(rid: str, user_id: str, approve: bool) -> str:
    """Returns the URL to redirect the browser to."""
    c = conn()
    try:
        r = c.execute("SELECT * FROM oauth_requests WHERE id=? AND user_id=?", (rid, user_id)).fetchone()
        if not r:
            raise OAuthError("invalid_request", "this approval link expired; start the connection again from your AI app")
        c.execute("DELETE FROM oauth_requests WHERE id=?", (rid,))
        p = json.loads(r["params"])
        sep = "&" if "?" in p["redirect_uri"] else "?"
        if not approve:
            return p["redirect_uri"] + sep + urlencode({"error": "access_denied", "state": p["state"]})
        code = secrets.token_urlsafe(32)
        c.execute("INSERT INTO oauth_codes(code_hash,client_id,user_id,redirect_uri,challenge,resource,expires_at) VALUES(?,?,?,?,?,?,?)",
                  (_h(code), p["client_id"], user_id, p["redirect_uri"], p["code_challenge"], p["resource"], _iso(_now() + CODE_TTL)))
        return p["redirect_uri"] + sep + urlencode({"code": code, "state": p["state"]})
    finally:
        c.close()


def _issue(c, client_id: str, user_id: str) -> dict:
    access, refresh = "sdo_" + secrets.token_urlsafe(32), "sdr_" + secrets.token_urlsafe(32)
    c.execute("""INSERT INTO oauth_tokens(id,client_id,user_id,access_hash,refresh_hash,access_expires,refresh_expires,created_at)
                 VALUES(?,?,?,?,?,?,?,?)""", ("ot_" + uuid.uuid4().hex[:12], client_id, user_id, _h(access), _h(refresh),
                                              _iso(_now() + ACCESS_TTL), _iso(_now() + REFRESH_TTL), _iso(_now())))
    return {"access_token": access, "token_type": "Bearer", "expires_in": int(ACCESS_TTL.total_seconds()), "refresh_token": refresh, "scope": SCOPE}


def token(form: dict) -> dict:
    gt = form.get("grant_type")
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        try:
            if gt == "authorization_code":
                r = c.execute("SELECT * FROM oauth_codes WHERE code_hash=?", (_h(form.get("code", "")),)).fetchone()
                if not r or r["used"] or r["expires_at"] < _iso(_now()):
                    raise OAuthError("invalid_grant", "code is invalid, used or expired")
                c.execute("UPDATE oauth_codes SET used=1 WHERE code_hash=?", (r["code_hash"],))
                c.execute("COMMIT")            # burned even if the checks below fail: one attempt per code
                c.execute("BEGIN IMMEDIATE")
                if form.get("client_id") != r["client_id"] or form.get("redirect_uri") != r["redirect_uri"]:
                    raise OAuthError("invalid_grant", "client_id or redirect_uri doesn't match the authorization")
                v = form.get("code_verifier", "")
                chal = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
                if not v or chal != r["challenge"]:
                    raise OAuthError("invalid_grant", "PKCE verification failed")
                out = _issue(c, r["client_id"], r["user_id"])
            elif gt == "refresh_token":
                r = c.execute("SELECT * FROM oauth_tokens WHERE refresh_hash=? AND revoked_at IS NULL", (_h(form.get("refresh_token", "")),)).fetchone()
                if not r or r["refresh_expires"] < _iso(_now()) or form.get("client_id", r["client_id"]) != r["client_id"]:
                    raise OAuthError("invalid_grant", "refresh token is invalid or expired")
                c.execute("UPDATE oauth_tokens SET revoked_at=? WHERE id=?", (_iso(_now()), r["id"]))   # rotation
                out = _issue(c, r["client_id"], r["user_id"])
            else:
                raise OAuthError("unsupported_grant_type", "grant_type must be authorization_code or refresh_token")
            c.execute("COMMIT")
            return out
        except Exception:
            c.execute("ROLLBACK") if c.in_transaction else None
            raise
    finally:
        c.close()


def user_for_access(tok: str) -> tuple[str, str] | None:
    c = conn()
    try:
        r = c.execute("SELECT * FROM oauth_tokens WHERE access_hash=? AND revoked_at IS NULL", (_h(tok),)).fetchone()
        if not r or r["access_expires"] < _iso(_now()):
            return None
        c.execute("UPDATE oauth_tokens SET last_used_at=? WHERE id=?", (_iso(_now()), r["id"]))
        return r["user_id"], f"oauth:{r['client_id']}"
    finally:
        c.close()


def revoke(tok: str) -> None:
    c = conn()
    try:
        c.execute("UPDATE oauth_tokens SET revoked_at=? WHERE (access_hash=? OR refresh_hash=?) AND revoked_at IS NULL", (_iso(_now()), _h(tok), _h(tok)))
    finally:
        c.close()


def connected_apps(user_id: str) -> list[dict]:
    c = conn()
    try:
        rows = c.execute("""SELECT t.client_id, c.name, MIN(t.created_at) AS first, MAX(t.last_used_at) AS last_used
                            FROM oauth_tokens t JOIN oauth_clients c ON c.client_id=t.client_id
                            WHERE t.user_id=? AND t.revoked_at IS NULL AND t.refresh_expires > ? GROUP BY t.client_id""",
                         (user_id, _iso(_now()))).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def disconnect(user_id: str, client_id: str) -> None:
    c = conn()
    try:
        c.execute("UPDATE oauth_tokens SET revoked_at=? WHERE user_id=? AND client_id=? AND revoked_at IS NULL", (_iso(_now()), user_id, client_id))
    finally:
        c.close()
