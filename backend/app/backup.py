"""Encrypted off-site backup of the data folder, so a host with a wiped disk (Render's free plan) keeps its data.

When SWINGDESK_BACKUP_REPO (owner/name of a PRIVATE GitHub repo) and SWINGDESK_BACKUP_TOKEN (a fine-grained token with
Contents read/write on that one repo) are set:
  * on startup, if the data folder is empty (fresh disk after a sleep or redeploy), the latest backup is restored;
  * a background thread checks every 20 s and, when something changed, uploads a new snapshot (at most once a minute,
    sooner after a sign-up); a final snapshot is taken on shutdown.
The snapshot is a tar.gz of every database (copied with SQLite's online backup, so it's consistent), settings and the
per-user encrypted secret stores, then encrypted with the master key (Fernet). GitHub only ever stores ciphertext.
Up to about a minute of changes can be lost if the instance is killed without a shutdown signal.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import sqlite3
import tarfile
import tempfile
import threading
import time
from pathlib import Path

import httpx

from . import db

API = "https://api.github.com"
PATH = "tickmark-data.bin"
TRANSPORT = None              # tests inject a fake GitHub
MIN_INTERVAL = 60
CHECK_EVERY = 20
log = logging.getLogger("tickmark.backup")


def enabled() -> bool:
    return bool(os.environ.get("SWINGDESK_BACKUP_REPO") and os.environ.get("SWINGDESK_BACKUP_TOKEN"))


def _client() -> httpx.Client:
    return httpx.Client(base_url=API, transport=TRANSPORT, timeout=30,
                        headers={"Authorization": f"Bearer {os.environ['SWINGDESK_BACKUP_TOKEN']}",
                                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})


def _fernet():
    from .credentials import EncryptedStore
    return EncryptedStore._fernet()


def _files() -> list[Path]:
    root = db.DATA_DIR
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file() and p.name != ".master_key"
            and not p.name.endswith(("-wal", "-shm", ".tmp", ".log"))]


def fingerprint() -> tuple:
    out = []
    for p in _files():
        st = p.stat()
        out.append((str(p.relative_to(db.DATA_DIR)), st.st_size, st.st_mtime_ns))
        if p.suffix == ".db":                     # WAL writes don't touch the .db mtime: include the WAL too
            w = p.with_name(p.name + "-wal")
            if w.exists():
                ws = w.stat()
                out.append((str(w.relative_to(db.DATA_DIR)), ws.st_size, ws.st_mtime_ns))
    return tuple(sorted(out))


def snapshot() -> bytes:
    """Encrypted tar.gz of the data folder."""
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in _files():
            rel = p.relative_to(db.DATA_DIR)
            if p.suffix == ".db":
                copy = Path(tmp) / rel.as_posix().replace("/", "__")
                src, dst = sqlite3.connect(p), sqlite3.connect(copy)
                try:
                    src.backup(dst)
                finally:
                    src.close()
                    dst.close()
                tar.add(copy, arcname=rel.as_posix())
            else:
                tar.add(p, arcname=rel.as_posix())
    return _fernet().encrypt(buf.getvalue())


def push() -> dict:
    data = snapshot()
    repo = os.environ["SWINGDESK_BACKUP_REPO"]
    with _client() as c:
        r = c.get(f"/repos/{repo}/contents/{PATH}")
        sha = r.json().get("sha") if r.status_code == 200 else None
        body = {"message": "Tickmark data backup", "content": base64.b64encode(data).decode()}
        if sha:
            body["sha"] = sha
        r = c.put(f"/repos/{repo}/contents/{PATH}", json=body)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"GitHub refused the backup ({r.status_code}): {r.text[:200]}")
    return {"bytes": len(data)}


def _is_empty() -> bool:
    return not any(p.suffix == ".db" for p in _files())


def restore_if_empty() -> str:
    """Bring back the latest snapshot onto a fresh disk. Never overwrites existing data."""
    if not enabled():
        return "backup off"
    if not _is_empty():
        return "data present; nothing restored"
    repo = os.environ["SWINGDESK_BACKUP_REPO"]
    with _client() as c:
        r = c.get(f"/repos/{repo}/contents/{PATH}", headers={"Accept": "application/vnd.github.raw"})
    if r.status_code == 404:
        return "no backup yet"
    if r.status_code != 200:
        raise RuntimeError(f"Couldn't read the backup ({r.status_code})")
    raw = _fernet().decrypt(r.content)
    root = db.DATA_DIR.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for m in tar.getmembers():                 # refuse anything that would land outside the data folder
            target = (root / m.name).resolve()
            if not str(target).startswith(str(root) + os.sep) or not (m.isfile() or m.isdir()):
                raise RuntimeError(f"unsafe path in backup: {m.name}")
        tar.extractall(root, filter="data")
    return "restored"


class Backup:
    def __init__(self):
        self.stop_ev = threading.Event()
        self.soon = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_fp: tuple | None = None
        self.last_push = 0.0
        self.state: dict = {"enabled": enabled()}

    def start(self):
        if not enabled() or self.thread:
            return
        self.last_fp = fingerprint()
        self.thread = threading.Thread(target=self._run, name="tickmark-backup", daemon=True)
        self.thread.start()

    def request(self):
        """Back up soon (e.g. right after a sign-up)."""
        self.soon.set()

    def maybe_push(self, force: bool = False):
        fp = fingerprint()
        if fp == self.last_fp and not force:
            return
        try:
            out = push()
            self.last_fp, self.last_push = fp, time.time()
            self.state = {"enabled": True, "last_ok": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **out}
        except Exception as exc:
            self.state = {"enabled": True, "last_error": f"{type(exc).__name__}: {exc}"[:200]}
            log.warning("backup failed: %s", exc)

    def _run(self):
        while not self.stop_ev.is_set():
            self.soon.wait(CHECK_EVERY)
            urgent = self.soon.is_set()
            self.soon.clear()
            if self.stop_ev.is_set():
                break
            if urgent or time.time() - self.last_push >= MIN_INTERVAL:
                self.maybe_push()

    def stop(self):
        self.stop_ev.set()
        self.soon.set()
        if enabled():
            self.maybe_push()          # final snapshot on shutdown (sleep, redeploy)


BACKUP = Backup()
