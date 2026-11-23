"""Sign in with GitHub (OAuth web flow). The server operator creates a GitHub OAuth app and sets
SWINGDESK_GITHUB_CLIENT_ID / SWINGDESK_GITHUB_CLIENT_SECRET (Render → Environment).
Callback URL to register on GitHub: <public url>/api/auth/github/callback. Scopes: read:user user:email (identity only).
"""
from __future__ import annotations

import os
import secrets
from urllib.parse import urlencode

import httpx

TRANSPORT = None          # tests inject a fake GitHub


class GitHubError(Exception):
    pass


def _cfg() -> tuple[str, str]:
    return os.environ.get("SWINGDESK_GITHUB_CLIENT_ID", "").strip(), os.environ.get("SWINGDESK_GITHUB_CLIENT_SECRET", "").strip()


def configured() -> bool:
    cid, sec = _cfg()
    return bool(cid and sec)


def authorize_url(base: str) -> tuple[str, str]:
    cid, _ = _cfg()
    state = secrets.token_urlsafe(24)
    q = urlencode({"client_id": cid, "redirect_uri": f"{base}/api/auth/github/callback", "scope": "read:user user:email",
                   "state": state, "allow_signup": "true"})
    return f"https://github.com/login/oauth/authorize?{q}", state


def fetch_user(code: str, base: str) -> dict:
    """Exchange the code, then read the GitHub id, name and primary verified email."""
    cid, sec = _cfg()
    if not code:
        raise GitHubError("GitHub didn't send a sign-in code. Try again.")
    with httpx.Client(transport=TRANSPORT, timeout=15) as c:
        r = c.post("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"},
                   data={"client_id": cid, "client_secret": sec, "code": code, "redirect_uri": f"{base}/api/auth/github/callback"})
        tok = (r.json() if r.status_code == 200 else {}).get("access_token")
        if not tok:
            raise GitHubError("GitHub didn't accept the sign-in. Try again.")
        h = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
        me = c.get("https://api.github.com/user", headers=h)
        if me.status_code != 200:
            raise GitHubError("Couldn't read your GitHub profile.")
        u = me.json()
        emails = c.get("https://api.github.com/user/emails", headers=h)
        primary = next((e["email"] for e in (emails.json() if emails.status_code == 200 else [])
                        if e.get("primary") and e.get("verified")), None)
    return {"id": str(u["id"]), "login": u.get("login"), "name": u.get("name") or u.get("login"), "email": primary}
