"""Multi-user portal: sign-in, invites, sessions, per-user data, and personal access tokens for MCP.

Off by default (the single-user local app). Turn on with SWINGDESK_PORTAL=on. Then:
  * every /api request needs a session (HttpOnly, SameSite=Lax cookie) and, for changes, the X-SwingDesk header
    (a cross-site form can't send it, which blocks CSRF);
  * each user gets their own folder: databases, settings and an encrypted secret store (credentials.EncryptedStore),
    so one user's broker keys, orders, journal and copilot history are never visible to another;
  * the first account becomes the admin; after that, sign-up needs an invite code from the admin
    (or SWINGDESK_OPEN_SIGNUP=on);
  * MCP clients authenticate with a personal access token (Bearer) that maps to its user.

Passwords: scrypt (n=2^14, r=8, p=1, 16-byte salt). Session and MCP tokens are stored only as SHA-256 hashes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import db

SESSION_COOKIE = "sd_session"
SESSION_DAYS = 30          # "Remember me" (and guests): the browser keeps the sign-in this long
SHORT_SESSION_DAYS = 1     # without "Remember me": a browser-session cookie, and the server forgets it after a day
GUEST_DAYS = 30            # an unused guest desk is removed after this long
MIN_PASSWORD = 10
LOCAL_USER = "local"                 # owner of MCP tokens in the single-user app
_fail: dict[str, list[float]] = {}
from contextvars import ContextVar  # noqa: E402
CURRENT_USER: ContextVar[str] = ContextVar("swingdesk_user", default="local")   # who is acting: a portal user id, or "local"
_lock = threading.Lock()


class PortalError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def enabled() -> bool:
    return os.environ.get("SWINGDESK_PORTAL", "off").lower() in ("on", "1", "true")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def conn() -> sqlite3.Connection:
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db.DATA_DIR / "portal.db", isolation_level=None, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, pw_hash TEXT NOT NULL, role TEXT NOT NULL,
      name TEXT, created_at TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL, last_seen TEXT);
    CREATE TABLE IF NOT EXISTS invites (code_hash TEXT PRIMARY KEY, created_by TEXT NOT NULL, email TEXT, created_at TEXT NOT NULL,
      used_by TEXT, used_at TEXT);
    CREATE TABLE IF NOT EXISTS mcp_tokens (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
      prefix TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT, revoked_at TEXT);
    """)
    return c


# ---------- passwords ----------

def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


# ---------- tenants ----------

def tenant_dir(user_id: str):
    if not re.fullmatch(r"u_[0-9a-f]{16}", user_id):
        raise PortalError("bad user id", 400)
    return db.DATA_DIR / "users" / user_id


@contextmanager
def as_user(user_id: str | None):
    """Run code with that user's data folder (None: the single-user app)."""
    tok = db.TENANT.set(tenant_dir(user_id) if user_id and user_id != LOCAL_USER else None)
    utok = CURRENT_USER.set(user_id or LOCAL_USER)
    try:
        yield
    finally:
        db.TENANT.reset(tok)
        CURRENT_USER.reset(utok)


def user_ids() -> list[str]:
    c = conn()
    try:
        return [r["id"] for r in c.execute("SELECT id FROM users WHERE disabled=0 ORDER BY created_at")]
    finally:
        c.close()


def for_each_tenant(fn) -> list:
    """Run fn() once per active user in the portal, or once for the single-user app. Errors are kept per user."""
    if not enabled():
        return [fn()]
    out = []
    for uid in user_ids():
        with as_user(uid):
            try:
                out.append(fn())
            except Exception as exc:
                out.append({"user": uid, "error": f"{type(exc).__name__}: {exc}"[:200]})
    return out


# ---------- accounts ----------

def _public(u) -> dict:
    return {"id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"], "created_at": u["created_at"]}


def real_user_count(c=None) -> int:
    own = c is None
    c = c or conn()
    try:
        return c.execute("SELECT COUNT(*) FROM users WHERE role!='guest'").fetchone()[0]
    finally:
        if own:
            c.close()


def create_guest() -> tuple[str, dict]:
    """A browser's own view-only desk: real prices, paper trades saved, nothing that touches money or keys."""
    c = conn()
    try:
        uid = "u_" + uuid.uuid4().hex[:16]
        c.execute("INSERT INTO users(id,email,pw_hash,role,name,created_at) VALUES(?,?,?,?,?,?)",
                  (uid, f"guest-{uid[2:]}@guest.invalid", "!", "guest", "Guest", _now()))
        token = secrets.token_urlsafe(32)
        exp = (datetime.now(timezone.utc) + timedelta(days=GUEST_DAYS)).isoformat(timespec="seconds")
        c.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)", (_h(token), uid, _now(), exp))
        return token, _public(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
    finally:
        c.close()


def cleanup_guests(days: int = GUEST_DAYS) -> int:
    """Remove guest desks nobody has opened for `days` (their sessions have all expired). Real accounts are never touched."""
    import shutil
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    c = conn()
    try:
        old = [r["id"] for r in c.execute("""SELECT u.id FROM users u WHERE u.role='guest' AND u.created_at < ? AND NOT EXISTS
                 (SELECT 1 FROM sessions s WHERE s.user_id=u.id AND (s.expires_at > ? OR COALESCE(s.last_seen, '') > ?))""",
                 (cutoff, _now(), cutoff))]
        for uid in old:
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            c.execute("DELETE FROM users WHERE id=? AND role='guest'", (uid,))
            shutil.rmtree(tenant_dir(uid), ignore_errors=True)
        return len(old)
    finally:
        c.close()


def signup(email: str, password: str, name: str = "", invite: str = "", guest_id: str | None = None) -> dict:
    email = (email or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise PortalError("Enter a valid email address.")
    if len(password or "") < MIN_PASSWORD:
        raise PortalError(f"Use a password of at least {MIN_PASSWORD} characters.")
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        first = real_user_count(c) == 0
        setup = os.environ.get("SWINGDESK_SETUP_CODE", "").strip()
        if first and setup and not hmac.compare_digest((invite or "").strip(), setup):
            c.execute("ROLLBACK")      # on a public server, only the operator (who can read the setup code) becomes admin
            raise PortalError("Enter the setup code from the server's settings to create the administrator account.", 403)
        inv = None
        if not first and os.environ.get("SWINGDESK_OPEN_SIGNUP", "off").lower() not in ("on", "1", "true"):
            inv = c.execute("SELECT * FROM invites WHERE code_hash=? AND used_by IS NULL", (_h((invite or "").strip()),)).fetchone()
            if not inv:
                c.execute("ROLLBACK")
                raise PortalError("Sign-up is by invitation. Ask the administrator for an invite code.", 403)
            if inv["email"] and inv["email"] != email:
                c.execute("ROLLBACK")
                raise PortalError("This invite is for a different email address.", 403)
        if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            c.execute("ROLLBACK")
            raise PortalError("An account with this email already exists.", 409)
        g = c.execute("SELECT id FROM users WHERE id=? AND role='guest'", (guest_id,)).fetchone() if guest_id else None
        if g:   # the guest becomes a real account and keeps its desk (paper trades, watchlist, journal)
            uid = g["id"]
            c.execute("UPDATE users SET email=?, pw_hash=?, role=?, name=? WHERE id=?",
                      (email, hash_password(password), "admin" if first else "user", name.strip()[:80] or None, uid))
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        else:
            uid = "u_" + uuid.uuid4().hex[:16]
            c.execute("INSERT INTO users(id,email,pw_hash,role,name,created_at) VALUES(?,?,?,?,?,?)",
                      (uid, email, hash_password(password), "admin" if first else "user", name.strip()[:80] or None, _now()))
        if inv:
            c.execute("UPDATE invites SET used_by=?, used_at=? WHERE code_hash=?", (uid, _now(), inv["code_hash"]))
        c.execute("COMMIT")
        return _public(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
    finally:
        c.close()


def _throttled(key: str) -> bool:
    with _lock:
        now = time.time()
        _fail[key] = [t for t in _fail.get(key, []) if now - t < 900]
        return len(_fail[key]) >= 5


def _failed(key: str) -> None:
    with _lock:
        _fail.setdefault(key, []).append(time.time())


def login(email: str, password: str, ip: str = "", remember: bool = True) -> tuple[str, dict]:
    email = (email or "").strip().lower()
    for key in (f"e:{email}", f"i:{ip}"):
        if _throttled(key):
            raise PortalError("Too many failed sign-ins. Try again in 15 minutes.", 429)
    c = conn()
    try:
        u = c.execute("SELECT * FROM users WHERE email=? AND disabled=0 AND role!='guest'", (email,)).fetchone()
        ok = check_password(password or "", u["pw_hash"]) if u else check_password(password or "", hash_password("x" * 12))
        if not (u and ok):
            _failed(f"e:{email}")
            _failed(f"i:{ip}")
            raise PortalError("Wrong email or password.", 401)
        token = secrets.token_urlsafe(32)
        exp = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS if remember else SHORT_SESSION_DAYS)).isoformat(timespec="seconds")
        c.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)", (_h(token), u["id"], _now(), exp))
        return token, _public(u)
    finally:
        c.close()


def logout(token: str | None) -> None:
    if token:
        c = conn()
        try:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_h(token),))
        finally:
            c.close()


def user_for_session(token: str | None) -> dict | None:
    if not token:
        return None
    c = conn()
    try:
        r = c.execute("""SELECT u.* , s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id
                         WHERE s.token_hash=? AND u.disabled=0""", (_h(token),)).fetchone()
        if not r or r["expires_at"] < _now():
            return None
        c.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (_now(), _h(token)))
        return _public(r)
    finally:
        c.close()


def create_invite(admin: dict, email: str = "") -> dict:
    if admin.get("role") != "admin":
        raise PortalError("Only the administrator can invite people.", 403)
    code = secrets.token_urlsafe(12)
    c = conn()
    try:
        c.execute("INSERT INTO invites(code_hash,created_by,email,created_at) VALUES(?,?,?,?)",
                  (_h(code), admin["id"], (email or "").strip().lower() or None, _now()))
    finally:
        c.close()
    return {"code": code, "email": email or None, "note": "Shown once. Send it to the person you're inviting."}


def list_users(admin: dict) -> list[dict]:
    if admin.get("role") != "admin":
        raise PortalError("Only the administrator can list users.", 403)
    c = conn()
    try:
        return [_public(r) | {"disabled": bool(r["disabled"])} for r in c.execute("SELECT * FROM users WHERE role!='guest' ORDER BY created_at")]
    finally:
        c.close()


# ---------- MCP personal access tokens ----------

def create_token(user_id: str, name: str) -> dict:
    token = "sdk_" + secrets.token_urlsafe(32)
    tid = "mt_" + uuid.uuid4().hex[:10]
    c = conn()
    try:
        c.execute("INSERT INTO mcp_tokens(id,user_id,name,token_hash,prefix,created_at) VALUES(?,?,?,?,?,?)",
                  (tid, user_id, (name or "AI app").strip()[:60], _h(token), token[:8], _now()))
    finally:
        c.close()
    return {"id": tid, "token": token, "name": name, "note": "Shown once. Paste it into your AI app's connector settings."}


def list_tokens(user_id: str) -> list[dict]:
    c = conn()
    try:
        return [dict(r) for r in c.execute("SELECT id,name,prefix,created_at,last_used_at,revoked_at FROM mcp_tokens WHERE user_id=? "
                                           "ORDER BY created_at DESC", (user_id,))]
    finally:
        c.close()


def revoke_token(user_id: str, tid: str) -> None:
    c = conn()
    try:
        n = c.execute("UPDATE mcp_tokens SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL", (_now(), tid, user_id)).rowcount
    finally:
        c.close()
    if not n:
        raise PortalError("Unknown or already revoked token.", 404)


def user_for_token(token: str | None) -> tuple[str, str] | None:
    """(user_id, token_id) for a valid personal access token."""
    if not token or not token.startswith("sdk_"):
        return None
    c = conn()
    try:
        r = c.execute("SELECT id,user_id FROM mcp_tokens WHERE token_hash=? AND revoked_at IS NULL", (_h(token),)).fetchone()
        if not r:
            return None
        if r["user_id"] != LOCAL_USER and not c.execute("SELECT 1 FROM users WHERE id=? AND disabled=0", (r["user_id"],)).fetchone():
            return None
        c.execute("UPDATE mcp_tokens SET last_used_at=? WHERE id=?", (_now(), r["id"]))
        return r["user_id"], r["id"]
    finally:
        c.close()
