"""Server-wide settings the administrator can change in the app (not per user).

Used for the free copilot plans: the server's AI keys and the open-model server. Environment variables (e.g. set in
Render's dashboard) always win; otherwise values saved by the admin are used. Secrets are encrypted at rest with the
master key (credentials.EncryptedStore in DATA_DIR/server); plain settings live in DATA_DIR/server/settings.json.
"""
from __future__ import annotations

import json
import os

from . import db

SECRETS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "SWINGDESK_OSS_API_KEY")
PLAIN = ("SWINGDESK_FREE_TIER", "SWINGDESK_OSS_BASE_URL", "SWINGDESK_OSS_MODELS")


def _dir():
    d = db.DATA_DIR / "server"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _store():
    from .credentials import EncryptedStore
    return EncryptedStore(_dir())


def _plain() -> dict:
    try:
        return json.loads((_dir() / "settings.json").read_text())
    except (OSError, ValueError):
        return {}


def get(name: str) -> str | None:
    """Environment first, then what the admin saved in the app."""
    if os.environ.get(name):
        return os.environ[name]
    if name in SECRETS:
        try:
            return _store().get_password("server", name)
        except Exception:
            return None
    return _plain().get(name)


def source(name: str) -> str | None:
    if os.environ.get(name):
        return "environment"
    return "app" if get(name) else None


def set(name: str, value: str | None) -> None:
    if name in SECRETS:
        st = _store()
        st.set_password("server", name, value) if value else st.delete_password("server", name)
    elif name in PLAIN:
        d = _plain()
        if value:
            d[name] = value
        else:
            d.pop(name, None)
        (_dir() / "settings.json").write_text(json.dumps(d))
    else:
        raise KeyError(name)
