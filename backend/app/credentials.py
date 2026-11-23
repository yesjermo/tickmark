"""Broker credentials, kept server-side only.

Stored in the operating system's keychain (macOS Keychain, Windows Credential
Locker, Secret Service on Linux) via `keyring`, never in SQLite, files, logs or
API responses. Environment variables still work and take priority, for servers
and CI. The browser only ever sees a masked key hint.
"""
from __future__ import annotations

import os

SERVICE = "swingdesk"
KEY_ID, SECRET = "alpaca_paper_key_id", "alpaca_paper_secret"
LIVE_KEY_ID, LIVE_SECRET = "alpaca_live_key_id", "alpaca_live_secret"   # stored separately; only usable through the live gate

_backend = None  # injectable for tests (anything with get_password/set_password/delete_password)


class EncryptedStore:
    """Per-user secret store for the hosted portal: one Fernet-encrypted JSON file in the user's data folder.

    The master key comes from SWINGDESK_MASTER_KEY (set it from your host's secret manager / KMS). Without it, a
    key file is generated next to the data (mode 0600) so a local portal still works; don't do that in production."""
    def __init__(self, folder):
        from pathlib import Path
        self.path = Path(folder) / "secrets.enc"

    @staticmethod
    def _fernet():
        from cryptography.fernet import Fernet
        from . import db
        k = os.environ.get("SWINGDESK_MASTER_KEY")
        if not k:
            kf = db.DATA_DIR / ".master_key"
            if not kf.exists():
                db.DATA_DIR.mkdir(parents=True, exist_ok=True)
                kf.write_bytes(Fernet.generate_key())
                os.chmod(kf, 0o600)
            k = kf.read_bytes().decode()
        return Fernet(k.encode() if isinstance(k, str) else k)

    def _load(self) -> dict:
        import json
        if not self.path.exists():
            return {}
        return json.loads(self._fernet().decrypt(self.path.read_bytes()))

    def _save(self, d: dict) -> None:
        import json
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(self._fernet().encrypt(json.dumps(d).encode()))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def get_password(self, service, name):
        return self._load().get(f"{service}:{name}")

    def set_password(self, service, name, value):
        d = self._load()
        d[f"{service}:{name}"] = value
        self._save(d)

    def delete_password(self, service, name):
        d = self._load()
        d.pop(f"{service}:{name}", None)
        self._save(d)


def backend():
    global _backend
    from . import db
    tenant = db.TENANT.get()
    if tenant is not None:                     # hosted portal: never the server's OS keychain, never shared between users
        return EncryptedStore(tenant)
    if _backend is None:
        import keyring
        _backend = keyring
    return _backend


def _names(env: str):
    return (LIVE_KEY_ID, LIVE_SECRET, "ALPACA_LIVE_API_KEY_ID", "ALPACA_LIVE_API_SECRET_KEY") if env == "real" else \
           (KEY_ID, SECRET, "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")


def _env_ok() -> bool:
    """Environment-variable credentials are a single-user convenience; never in the multi-user portal."""
    from . import db
    return db.TENANT.get() is None


def get_alpaca(env: str = "paper") -> tuple[str | None, str | None, str | None]:
    """(key_id, secret, source) for the paper or real-money account."""
    kn, sn, ke, se = _names(env)
    k, s = (os.environ.get(ke), os.environ.get(se)) if _env_ok() else (None, None)
    if k and s:
        return k, s, "environment"
    try:
        k, s = backend().get_password(SERVICE, kn), backend().get_password(SERVICE, sn)
    except Exception:
        return None, None, None
    return (k, s, "keychain") if k and s else (None, None, None)


def save_alpaca(key_id: str, secret: str, env: str = "paper") -> None:
    kn, sn, _, _ = _names(env)
    backend().set_password(SERVICE, kn, key_id)
    backend().set_password(SERVICE, sn, secret)


def delete_alpaca(env: str = "paper") -> None:
    kn, sn, _, _ = _names(env)
    for name in (kn, sn):
        try:
            backend().delete_password(SERVICE, name)
        except Exception:
            pass


def get_secret(name: str) -> str | None:
    try:
        return backend().get_password(SERVICE, name)
    except Exception:
        return None


def set_secret(name: str, value: str | None) -> None:
    if value:
        backend().set_password(SERVICE, name, value)
    else:
        try:
            backend().delete_password(SERVICE, name)
        except Exception:
            pass


def hint(key_id: str | None) -> str | None:
    return None if not key_id else f"{key_id[:2]}…{key_id[-4:]}"
