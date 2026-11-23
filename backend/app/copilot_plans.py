"""Copilot plans, like an IDE copilot's free tier: sign in, get a monthly allowance, no API key needed.

  free         Claude on the operator's Anthropic key        (ANTHROPIC_API_KEY, SWINGDESK_FREE_MESSAGES=50,
                                                              SWINGDESK_FREE_MODEL=claude-opus-5-5)
  free_gpt     OpenAI GPT on the operator's OpenAI key        (OPENAI_API_KEY, SWINGDESK_FREE_GPT_MESSAGES=50,
                                                              SWINGDESK_FREE_GPT_MODEL, optional)
  free_gemini  Google Gemini on the operator's Gemini key     (GEMINI_API_KEY or GOOGLE_API_KEY, SWINGDESK_FREE_GEMINI_MESSAGES=100,
                                                              SWINGDESK_FREE_GEMINI_MODEL, optional)
  oss          an open-source model: Ollama on the server, or a hosted open-model service with a key
               (SWINGDESK_OSS_BASE_URL, SWINGDESK_OSS_MODEL, optional SWINGDESK_OSS_API_KEY, SWINGDESK_OSS_DAILY=300)

The three free plans are offered only when SWINGDESK_FREE_TIER=on and that provider's key is set. Without a pinned
GPT/Gemini model, the newest flagship the operator's key can use is picked from the provider's model list (cached).
Unlimited use: the user's own Claude, OpenAI or Gemini key, or their own local model server.

A "message" is one question from the user, however many lookups the copilot makes to answer it. Usage is metered per
signed-in user in portal.db ("local" in the single-user app), so it follows the user across devices and restarts.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from . import portal

PLANS = {
    "free": {"name": "Free · Claude", "provider": "anthropic", "key_env": ("ANTHROPIC_API_KEY",), "limit_env": "SWINGDESK_FREE_MESSAGES",
             "limit": 50, "model_env": "SWINGDESK_FREE_MODEL", "default_model": "claude-opus-5-5", "period": "month"},
    "free_gpt": {"name": "Free · GPT", "provider": "openai", "key_env": ("OPENAI_API_KEY",), "limit_env": "SWINGDESK_FREE_GPT_MESSAGES",
                 "limit": 50, "model_env": "SWINGDESK_FREE_GPT_MODEL", "default_model": None, "period": "month"},
    "free_gemini": {"name": "Free · Gemini", "provider": "gemini", "key_env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
                    "limit_env": "SWINGDESK_FREE_GEMINI_MESSAGES", "limit": 100, "model_env": "SWINGDESK_FREE_GEMINI_MODEL",
                    "default_model": None, "period": "month"},
    "oss": {"name": "Open-source · free", "provider": "oss", "key_env": (), "limit_env": "SWINGDESK_OSS_DAILY", "limit": 300,
            "model_env": "SWINGDESK_OSS_MODEL", "default_model": "qwen3.5:9b", "period": "day"},
}
ORDER = ("free", "free_gpt", "free_gemini", "oss")
_model_cache: dict[str, tuple[float, str]] = {}
CACHE_SECONDS = 6 * 3600


def _on(name: str, default: str = "off") -> bool:
    return os.environ.get(name, default).lower() in ("on", "1", "true")


def operator_key(plan: str) -> str | None:
    return next((os.environ[k] for k in PLANS[plan]["key_env"] if os.environ.get(k)), None)


def _enabled(plan: str) -> bool:
    if plan == "oss":
        return bool(os.environ.get("SWINGDESK_OSS_BASE_URL"))
    return _on("SWINGDESK_FREE_TIER") and bool(operator_key(plan))


def config() -> dict:
    out = {}
    for plan, p in PLANS.items():
        out[plan] = {"enabled": _enabled(plan), "limit": int(os.environ.get(p["limit_env"], str(p["limit"]))), "period": p["period"],
                     "model": os.environ.get(p["model_env"]) or p["default_model"] or (_model_cache.get(plan, (0, None))[1]),
                     "provider": p["provider"]}
    out["oss"]["base_url"] = os.environ.get("SWINGDESK_OSS_BASE_URL", "")
    return out


def model_for(plan: str) -> str:
    """The model a plan runs: pinned by the operator, or the newest flagship the operator's key can use (cached)."""
    from .copilot import CopilotError, cloud_models
    pinned = os.environ.get(PLANS[plan]["model_env"]) or PLANS[plan]["default_model"]
    if pinned:
        return pinned
    hit = _model_cache.get(plan)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    models = cloud_models(PLANS[plan]["provider"], operator_key(plan))
    if not models:
        raise CopilotError(f"The server's {PLANS[plan]['name']} key can't use any chat model right now.", 502)
    _model_cache[plan] = (time.time(), models[0])
    return models[0]


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
    cfg = config()
    name = PLANS[plan]["name"]
    if not cfg[plan]["enabled"]:
        others = [PLANS[p]["name"] for p in ORDER if p != plan and cfg[p]["enabled"]]
        raise CopilotError(f"{name} isn't offered on this server." + (f" Try {', '.join(others)}," if others else "")
                           + " or add your own Claude, OpenAI or Gemini key.", 409)
    if used(plan) >= cfg[plan]["limit"]:
        when = "next month" if cfg[plan]["period"] == "month" else "tomorrow (UTC)"
        others = [PLANS[p]["name"] for p in ORDER if p != plan and cfg[p]["enabled"] and used(p) < cfg[p]["limit"]]
        raise CopilotError(f"You've used all {cfg[plan]['limit']} {name} messages this {cfg[plan]['period']}. It resets {when}. "
                           + (f"You still have {', '.join(others)}; " if others else "")
                           + "your own key gives unlimited use.", 429)


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
    for plan in ORDER:
        p, c = PLANS[plan], cfg[plan]
        model = c["model"] or ("newest GPT" if plan == "free_gpt" else "newest Gemini" if plan == "free_gemini" else "")
        note = (f"{c['model'] or 'the newest model'} hosted by this server, free (fair use {c['limit']}/day)" if plan == "oss" else
                f"{c['limit']} messages a month with {model}, paid for by this server")
        out.append({"id": plan, "enabled": c["enabled"], "limit": c["limit"], "period": c["period"],
                    "used": used(plan) if c["enabled"] else 0, "model": model, "name": p["name"], "note": note})
    return out
