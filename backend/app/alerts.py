"""Alerts: an in-app feed, plus optional desktop, Slack and email delivery.

Events are recorded inside the transaction that caused them (emit), and delivered
afterwards (flush), so a slow Slack or mail server never holds a database lock.
Each event has a de-duplication key, so the same fill or reconciliation break is
announced once. Secrets (Slack webhook URL, SMTP password) live in the system
keychain; channel settings live in settings.json.

Severity: info (signals, fills), warn (stops, failed fetches, kill switch),
critical (reconciliation breaks, exits that could not be sent, scheduler errors).
External channels only receive alerts at or above their minimum severity.
"""
from __future__ import annotations

import json
import smtplib
import subprocess
import sys
from datetime import datetime, timezone
from email.message import EmailMessage

import httpx

from . import credentials, db

SEVERITY = {"info": 0, "warn": 1, "critical": 2}
DEFAULTS = {"desktop": False, "slack": False, "email": False, "min_severity": "warn",
            "email_to": "", "smtp_host": "", "smtp_port": 587, "smtp_user": ""}
SLACK_SECRET, SMTP_SECRET = "slack_webhook_url", "smtp_password"
SLACK_TRANSPORT: httpx.BaseTransport | None = None   # injectable for tests
SMTP_FACTORY = smtplib.SMTP                          # injectable for tests
_runner = subprocess.run                              # injectable for tests


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def settings() -> dict:
    return {**DEFAULTS, **(db.get_setting("alerts", {}) or {})}


def save_settings(new: dict) -> dict:
    s = settings()
    for k in DEFAULTS:
        if k in new:
            s[k] = new[k]
    if s["min_severity"] not in SEVERITY:
        raise ValueError("min_severity must be info, warn or critical")
    s["smtp_port"] = int(s["smtp_port"] or 587)
    db.set_setting("alerts", s)
    return public_settings()


def public_settings() -> dict:
    """Settings safe to send to the browser: secrets reduced to 'is it set'."""
    return {**settings(), "has_slack_webhook": bool(credentials.get_secret(SLACK_SECRET)),
            "has_smtp_password": bool(credentials.get_secret(SMTP_SECRET)), "desktop_supported": sys.platform == "darwin"}


def emit(conn, kind: str, title: str, body: str = "", severity: str = "info", symbol: str | None = None,
         key: str | None = None) -> None:
    conn.execute("INSERT OR IGNORE INTO alerts(at,kind,severity,title,body,symbol,dedup_key) VALUES(?,?,?,?,?,?,?)",
                 (_now(), kind, severity, title, body, symbol, key))


def feed(conn, limit: int = 50) -> dict:
    rows = [dict(r) for r in conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))]
    for r in rows:
        r["delivered"] = json.loads(r.pop("delivered_json") or "{}")
    unread = conn.execute("SELECT COUNT(*) FROM alerts WHERE read=0").fetchone()[0]
    return {"alerts": rows, "unread": unread}


def mark_read(conn) -> None:
    conn.execute("UPDATE alerts SET read=1 WHERE read=0")


# ---------- delivery ----------

def _desktop(title: str, body: str) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("desktop notifications are only implemented for macOS")
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')[:240]  # noqa: E731
    _runner(["osascript", "-e", f'display notification "{esc(body)}" with title "Tickmark" subtitle "{esc(title)}"'],
            check=True, timeout=10, capture_output=True)


def _slack(title: str, body: str) -> None:
    url = credentials.get_secret(SLACK_SECRET)
    if not url:
        raise RuntimeError("no Slack webhook saved")
    if not url.startswith("https://hooks.slack.com/"):
        raise RuntimeError("Slack webhook must start with https://hooks.slack.com/")
    with httpx.Client(transport=SLACK_TRANSPORT, timeout=10) as c:
        r = c.post(url, json={"text": f"*{title}*\n{body}"})
        r.raise_for_status()


def _email(title: str, body: str) -> None:
    s = settings()
    pw = credentials.get_secret(SMTP_SECRET)
    if not (s["smtp_host"] and s["smtp_user"] and s["email_to"] and pw):
        raise RuntimeError("email needs SMTP host, user, password and a To address")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = f"Tickmark: {title}", s["smtp_user"], s["email_to"]
    msg.set_content(body or title)
    with SMTP_FACTORY(s["smtp_host"], s["smtp_port"], timeout=15) as smtp:
        smtp.starttls()
        smtp.login(s["smtp_user"], pw)
        smtp.send_message(msg)


CHANNELS = {"desktop": _desktop, "slack": _slack, "email": _email}


def send(channel: str, title: str, body: str) -> None:
    CHANNELS[channel](title, body)


def flush(mode: str | None = None) -> int:
    """Deliver undelivered alerts to enabled channels. Safe to call often; never raises."""
    try:
        c = db.connect(mode)
        try:
            rows = [dict(r) for r in c.execute("SELECT * FROM alerts WHERE delivered_json IS NULL ORDER BY id LIMIT 50")]
        finally:
            c.close()
    except Exception:
        return 0
    if not rows:
        return 0
    s = settings()
    active = [ch for ch in CHANNELS if s.get(ch)]
    results = {}
    for r in rows:
        out = {}
        if SEVERITY.get(r["severity"], 0) >= SEVERITY[s["min_severity"]]:
            for ch in active:
                try:
                    send(ch, r["title"], r["body"] or "")
                    out[ch] = "sent"
                except Exception as exc:
                    out[ch] = f"failed: {type(exc).__name__}: {exc}"[:120]
        results[r["id"]] = out
    try:
        c = db.connect(mode)
        try:
            c.execute("BEGIN IMMEDIATE")
            for aid, out in results.items():
                c.execute("UPDATE alerts SET delivered_json=? WHERE id=?", (json.dumps(out), aid))
            c.execute("COMMIT")
        finally:
            c.close()
    except Exception:
        pass
    return len(rows)
