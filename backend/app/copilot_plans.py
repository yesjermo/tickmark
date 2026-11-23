"""Copilot plans, like an IDE copilot's free and paid tiers.

  free   Claude on the operator's API key, a monthly allowance of messages per user
         (SWINGDESK_FREE_TIER=on, SWINGDESK_FREE_MESSAGES=50, SWINGDESK_FREE_MODEL=claude-opus-5-5).
  oss    an open-source model the operator hosts (Ollama, vLLM, llama.cpp: any OpenAI-compatible server),
         free for everyone (SWINGDESK_OSS_BASE_URL, SWINGDESK_OSS_MODEL, fair-use SWINGDESK_OSS_DAILY=300).
  own    the user's own Anthropic API key (any Claude model) or their own local model server: unlimited, they pay
         their provider directly.

A "message" is one question from the user, however many lookups the copilot makes to answer it. Usage is metered
per user in portal.db, so it survives restarts and is shared by every device the user signs in from.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from . import portal


def _on(name: str, default: str = "off") -> bool:
    return os.environ.get(name, default).lower() in ("on", "1", "true")


def config() -> dict:
    return {
        "free": {"enabled": _on("SWINGDESK_FREE_TIER") and bool(os.environ.get("ANTHROPIC_API_KEY")),
                 "limit": int(os.environ.get("SWINGDESK_FREE_MESSAGES", "50")), "period": "month",
                 "model": os.environ.get("SWINGDESK_FREE_MODEL", "claude-opus-5-5")},
        "oss": {"enabled": bool(os.environ.get("SWINGDESK_OSS_BASE_URL")), "base_url": os.environ.get("SWINGDESK_OSS_BASE_URL", ""),
                "model": os.environ.get("SWINGDESK_OSS_MODEL", "qwen3.5:9b"), "limit": int(os.environ.get("SWINGDESK_OSS_DAILY", "300")),
                "period": "day"},
    }


def _period(kind: str) -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m") if kind == "month" else now.strftime("%Y-%m-%d")


def _ensure(c):
    c.execute("""CREATE TABLE IF NOT EXISTS usage (user_id TEXT NOT NULL, plan TEXT NOT NULL, period TEXT NOT NULL,
                 messages INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                 PRIMARY KEY (user_id, plan, period))""")


def used(plan: str, user: str | None = None) -> int:
    cfg = config()[plan]
    c = portal.conn()
    try:
        _ensure(c)
        r = c.execute("SELECT messages FROM usage WHERE user_id=? AND plan=? AND period=?",
                      (user or portal.CURRENT_USER.get(), plan, _period(cfg["period"]))).fetchone()
        return r["messages"] if r else 0
    finally:
        c.close()


def check(plan: str) -> None:
    from .copilot import CopilotError
    cfg = config()[plan]
    if not cfg["enabled"]:
        raise CopilotError({"free": "The free Claude plan isn't offered on this server. Add your own Anthropic key, or use the open-source model.",
                            "oss": "No open-source model is hosted on this server. Use your own local model server or a Claude key."}[plan], 409)
    n = used(plan)
    if n >= cfg["limit"]:
        when = "next month" if cfg["period"] == "month" else "tomorrow (UTC)"
        raise CopilotError(f"You've used all {cfg['limit']} {'free Claude' if plan == 'free' else 'open-source'} messages this "
                           f"{cfg['period']}. It resets {when}. Switch to the {'open-source model' if plan == 'free' else 'free Claude plan'} "
                           "or add your own key for unlimited use.", 429)


def record(plan: str, usage: dict) -> None:
    cfg = config()[plan]
    c = portal.conn()
    try:
        _ensure(c)
        c.execute("""INSERT INTO usage(user_id,plan,period,messages,input_tokens,output_tokens) VALUES(?,?,?,1,?,?)
                     ON CONFLICT(user_id,plan,period) DO UPDATE SET messages=messages+1, input_tokens=input_tokens+excluded.input_tokens,
                     output_tokens=output_tokens+excluded.output_tokens""",
                  (portal.CURRENT_USER.get(), plan, _period(cfg["period"]), int(usage.get("input") or 0), int(usage.get("output") or 0)))
    finally:
        c.close()


def summary() -> list[dict]:
    cfg = config()
    out = []
    for plan in ("free", "oss"):
        p = cfg[plan]
        out.append({"id": plan, "enabled": p["enabled"], "limit": p["limit"], "period": p["period"],
                    "used": used(plan) if p["enabled"] else 0, "model": p["model"],
                    "name": "Free · Claude" if plan == "free" else "Open-source · free",
                    "note": (f"{p['limit']} messages a month with {p['model']}, on this server's key" if plan == "free"
                             else f"{p['model']} hosted by this server, free (fair use {p['limit']}/day)")})
    return out
