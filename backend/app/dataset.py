"""Admin export: one SQL dataset of every account and everything people entered, as a single SQLite file (or CSVs in a zip).

Included: accounts (no password hashes), copilot usage, and from every desk (live / demo / real-money) the tables people fill in by
using the app: watchlist and account settings, rule sets, previews, orders, fills, journal, copilot chats and drafts, alerts, risk
limits and overrides, TradingView signals, value history, day-trading rules, crypto orders, plus each desk's settings.
Never included: password hashes, sessions, invite codes, API tokens, broker/exchange/AI keys (they live in the encrypted store and
are never in these databases), and as a second guard any column or setting whose name looks like a secret.
Market data the app downloads (price bars, earnings dates, sectors) is left out: it's public and large, not something users entered.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import db, portal

SECRETISH = re.compile(r"(secret|token|password|passwd|pw_hash|api[_-]?key|private[_-]?key|passphrase|seed|credential)", re.I)
DESK_TABLES = ("alerts", "copilot_proposals", "copilot_threads", "crypto_orders", "day_rulesets", "equity_history", "fills",
               "forward_signals", "journal", "journal_outcomes", "orders", "previews", "risk_limits", "risk_overrides", "rulesets",
               "tv_signals")
KV_KEYS = ("watchlist", "account", "broker_mode")
DESKS = {"swingdesk-live.db": "live", "swingdesk-demo.db": "backtest", "swingdesk-real-money.db": "real_money"}


def _redact(value):
    """Drop secret-looking keys anywhere inside JSON-like settings."""
    if isinstance(value, dict):
        return {k: ("[removed]" if SECRETISH.search(str(k)) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class _Out:
    def __init__(self, path: Path):
        self.c = sqlite3.connect(path)
        self.cols: dict[str, list[str]] = {}

    def add(self, table: str, row: dict):
        row = {k: v for k, v in row.items() if not SECRETISH.search(k)}
        have = self.cols.get(table)
        if have is None:
            self.c.execute(f'CREATE TABLE {_q(table)} ({", ".join(_q(k) for k in row)})')
            have = self.cols[table] = list(row)
        for k in row:
            if k not in have:                 # desks created by older versions may have fewer columns
                self.c.execute(f"ALTER TABLE {_q(table)} ADD COLUMN {_q(k)}")
                have.append(k)
        self.c.execute(f'INSERT INTO {_q(table)} ({", ".join(_q(k) for k in row)}) VALUES ({", ".join("?" for _ in row)})',
                       [json.dumps(v) if isinstance(v, (dict, list)) else v for v in row.values()])


def _desk_dirs() -> list[tuple[str, Path]]:
    """(user id, data folder) for every desk: each portal user, or the single-user app's own folder."""
    if not portal.enabled():
        return [(portal.LOCAL_USER, db.DATA_DIR)]
    return [(uid, portal.tenant_dir(uid)) for uid in portal.user_ids()]


def build(path: Path, include_guests: bool = True) -> dict:
    out = _Out(path)
    roles: dict[str, str] = {}
    if (db.DATA_DIR / "portal.db").exists():
        c = portal.conn()
        try:
            last = {r["user_id"]: r["seen"] for r in c.execute(
                "SELECT user_id, MAX(COALESCE(last_seen, created_at)) seen FROM sessions GROUP BY user_id")}
            cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
            for u in c.execute("SELECT * FROM users ORDER BY created_at"):
                roles[u["id"]] = u["role"]
                if u["role"] == "guest" and not include_guests:
                    continue
                out.add("accounts", {"user_id": u["id"], "email": None if u["role"] == "guest" else u["email"], "name": u["name"],
                                     "role": u["role"], "created_at": u["created_at"], "disabled": u["disabled"],
                                     "github_linked": bool(u["github_id"]) if "github_id" in cols else False,
                                     "google_linked": bool(u["google_id"]) if "google_id" in cols else False,
                                     "last_seen": last.get(u["id"])})
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='usage'").fetchone():
                for r in c.execute("SELECT * FROM usage"):
                    if include_guests or roles.get(r["user_id"]) != "guest":
                        out.add("copilot_usage", dict(r))
        finally:
            c.close()
    for uid, folder in _desk_dirs():
        if not include_guests and roles.get(uid) == "guest":
            continue
        sf = folder / "settings.json"
        if sf.exists():
            try:
                for k, v in _redact(json.loads(sf.read_text())).items():
                    if not SECRETISH.search(k):
                        out.add("desk_settings", {"user_id": uid, "key": k, "value": json.dumps(v)})
            except ValueError:
                pass
        for fname, desk in DESKS.items():
            f = folder / fname
            if not f.exists():
                continue
            src = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
            src.row_factory = sqlite3.Row
            try:
                present = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for t in DESK_TABLES:
                    if t in present:
                        for r in src.execute(f"SELECT * FROM {_q(t)}"):
                            out.add(t, {"user_id": uid, "desk": desk, **dict(r)})
                if "kv" in present:
                    for r in src.execute(f"SELECT key, value FROM kv WHERE key IN ({','.join('?' for _ in KV_KEYS)})", KV_KEYS):
                        out.add("desk_values", {"user_id": uid, "desk": desk, "key": r["key"], "value": r["value"]})
            finally:
                src.close()
    out.add("about", {"exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "guests_included": include_guests,
                      "note": "Tickmark dataset. No passwords, sessions, invites, tokens or keys. Market price data not included."})
    out.c.commit()
    tables = {t: out.c.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()[0] for t in out.cols}
    out.c.close()
    return tables


def sqlite_bytes(include_guests: bool = True) -> tuple[bytes, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "dataset.sqlite"
        tables = build(p, include_guests)
        return p.read_bytes(), tables


def csv_zip_bytes(include_guests: bool = True) -> tuple[bytes, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "dataset.sqlite"
        tables = build(p, include_guests)
        c = sqlite3.connect(p)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for t in tables:
                cur = c.execute(f"SELECT * FROM {_q(t)}")
                s = io.StringIO()
                w = csv.writer(s)
                w.writerow([d[0] for d in cur.description])
                w.writerows(cur)
                z.writestr(f"{t}.csv", s.getvalue())
        c.close()
        return buf.getvalue(), tables
