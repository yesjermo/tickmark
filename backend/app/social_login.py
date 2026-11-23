"""Sign in with GitHub or Google (OAuth 2.0 web flow). Identity only: name, user id and verified email.

The server owner registers an OAuth app with each provider and sets its client id and secret:
  GitHub  SWINGDESK_GITHUB_CLIENT_ID / SWINGDESK_GITHUB_CLIENT_SECRET   (github.com → Settings → Developer settings → OAuth Apps)
  Google  SWINGDESK_GOOGLE_CLIENT_ID / SWINGDESK_GOOGLE_CLIENT_SECRET   (Google Cloud console → APIs & Services → Credentials → OAuth client ID, "Web application")
Redirect / callback URL for both: <public url>/api/auth/<provider>/callback
"""
from __future__ import annotations

import os
import secrets
from urllib.parse import urlencode

import httpx

TRANSPORT = None          # tests inject a fake provider

PROVIDERS = {
    "github": {"name": "GitHub", "env": "GITHUB", "authorize": "https://github.com/login/oauth/authorize",
               "token": "https://github.com/login/oauth/access_token", "scope": "read:user user:email"},
    "google": {"name": "Google", "env": "GOOGLE", "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
               "token": "https://oauth2.googleapis.com/token", "scope": "openid email profile"},
}


class LoginError(Exception):
    pass


def _cfg(p: str) -> tuple[str, str]:
    e = PROVIDERS[p]["env"]
    return os.environ.get(f"SWINGDESK_{e}_CLIENT_ID", "").strip(), os.environ.get(f"SWINGDESK_{e}_CLIENT_SECRET", "").strip()


def configured(p: str) -> bool:
    cid, sec = _cfg(p)
    return bool(cid and sec)


def available() -> list[str]:
    return [p for p in PROVIDERS if configured(p)]


def authorize_url(p: str, base: str) -> tuple[str, str]:
    cid, _ = _cfg(p)
    state = secrets.token_urlsafe(24)
    q = {"client_id": cid, "redirect_uri": f"{base}/api/auth/{p}/callback", "scope": PROVIDERS[p]["scope"], "state": state}
    if p == "github":
        q["allow_signup"] = "true"
    else:
        q.update({"response_type": "code", "prompt": "select_account", "access_type": "online"})
    return f"{PROVIDERS[p]['authorize']}?{urlencode(q)}", state


def fetch_user(p: str, code: str, base: str) -> dict:
    """Exchange the code; return {id, name, email (verified only, else None)}."""
    cid, sec = _cfg(p)
    who = PROVIDERS[p]["name"]
    if not code:
        raise LoginError(f"{who} didn't send a sign-in code. Try again.")
    with httpx.Client(transport=TRANSPORT, timeout=15) as c:
        r = c.post(PROVIDERS[p]["token"], headers={"Accept": "application/json"},
                   data={"client_id": cid, "client_secret": sec, "code": code, "redirect_uri": f"{base}/api/auth/{p}/callback",
                         **({"grant_type": "authorization_code"} if p == "google" else {})})
        tok = (r.json() if r.status_code == 200 else {}).get("access_token")
        if not tok:
            raise LoginError(f"{who} didn't accept the sign-in. Try again.")
        if p == "google":
            me = c.get("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": f"Bearer {tok}"})
            if me.status_code != 200:
                raise LoginError("Couldn't read your Google profile.")
            u = me.json()
            return {"id": str(u["sub"]), "name": u.get("name"), "email": u.get("email") if u.get("email_verified") else None}
        h = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
        me = c.get("https://api.github.com/user", headers=h)
        if me.status_code != 200:
            raise LoginError("Couldn't read your GitHub profile.")
        u = me.json()
        emails = c.get("https://api.github.com/user/emails", headers=h)
        primary = next((e["email"] for e in (emails.json() if emails.status_code == 200 else [])
                        if e.get("primary") and e.get("verified")), None)
        return {"id": str(u["id"]), "name": u.get("name") or u.get("login"), "email": primary}
