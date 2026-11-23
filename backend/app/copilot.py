"""Tickmark Copilot: a chat assistant on Claude that reads the desk and drafts proposals.

What it can do
  * Read: the watchlist screen, rule evidence, indicators, strategies, positions and
    orders, the journal, risk limits, the forward test and the day-trading session.
    Every number it quotes comes from these tool results (the same code the UI uses).
  * Propose: a rule change, a risk-limit change, a swing order, a day trade, or an exit.
    A proposal is only a row in copilot_proposals. Nothing changes until the user
    clicks Apply (rules, limits, exits) or opens the ticket and confirms there (orders).

What it can never do
  * submit, cancel or close an order itself, override a risk block, touch the kill
    switch, change broker mode or environment, or pass the live-money gate. Those
    functions are simply not reachable from any tool below.

Models: Claude (Fable 5.2, Opus 5.5 default, Sonnet 5.5, Haiku 5.5) through the Messages API, or a
local open-weights model (e.g. Qwen 3.5 in Ollama or LM Studio) through its OpenAI-compatible chat
endpoint. Both use the same tools and the same proposal guardrails; the tool loop is run here and the
conversation is persisted in SQLite between HTTP requests. Prompt caching covers tools + system +
history; server-side refusal fallback is on. The API key comes from the OS keychain
(set in the app) or the standard ANTHROPIC_API_KEY / `ant auth login` credentials.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

from . import credentials, db, server_settings

MODEL = "claude-opus-5-5"     # default
EFFORT = os.environ.get("SWINGDESK_COPILOT_EFFORT", "medium")
MAX_STEPS = 12
KEY_NAME = "anthropic_api_key"
CLIENT_FACTORY = None          # tests inject a fake Claude client here
LOCAL_TRANSPORT = None         # tests inject an httpx transport for the local model server
DEFAULT_LOCAL = {"local_base_url": "http://localhost:11434/v1", "local_model": "qwen3.5:9b"}

# Claude models the user can pick. Prices are Anthropic list prices per million tokens (input / output).
CLAUDE_MODELS = {
    "claude-fable-5-2": {"name": "Claude Fable 5.2", "price": "$8 / $40", "fallbacks": True,
                         "note": "Anthropic's most capable model; strongest multi-step reasoning, slowest and priciest."},
    "claude-opus-5-5": {"name": "Claude Opus 5.5", "price": "$4 / $20", "fallbacks": True,
                        "note": "Recommended default: strong judgement and tool use at a moderate price."},
    "claude-sonnet-5-5": {"name": "Claude Sonnet 5.5", "price": "$2 / $10", "fallbacks": True,
                          "note": "Faster and cheaper; good for everyday lookups and reviews."},
    "claude-haiku-5-5": {"name": "Claude Haiku 5.5", "price": "$0.10 / $0.50", "fallbacks": False,
                         "note": "Quickest and cheapest; fine for simple questions, weaker on multi-step decisions."},
}


class CopilotError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


SYSTEM = """You are Tickmark Copilot, the assistant inside a swing- and day-trading research desk.

How you work
- Use the tools to look things up before answering. Every price, level, percentage or count you state must come from a tool result in this conversation; if a number isn't available, say so instead of estimating.
- The desk's rules engine decides whether a setup qualifies. Explain its verdicts with the rule evidence; never claim a setup qualifies when the screen says it doesn't.
- You cannot place, cancel or close orders, override risk checks, or change broker or account settings. When an action would help, call a propose_* tool. The user reviews and confirms every proposal in the app; say that plainly instead of implying anything was done.
- Propose only when the user asks for a change or a trade idea, or when the evidence clearly calls for one (for example, a position whose stop is missing). One proposal per idea; explain the trade-off and the risk in the rationale.
- Respect the risk engine: if a preview shows a hard block, say why and don't look for ways around it. Never propose loosening a limit just to make a specific trade pass.
- You are a decision-support tool, not a licensed adviser. Present evidence, risks and alternatives; the user decides.

Style
- Lead with the answer. Keep replies short: a few sentences or a compact list. Use the user's level (beginner: plain words; analyst: terse, with thresholds and values).
- Name dates and the data source when you cite prices (daily bars are completed sessions; intraday bars are 5-minute)."""


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


SYM = {"type": "string", "description": "Ticker symbol, e.g. NVDA"}
WHY = {"type": "string", "description": "Why, citing the evidence from tool results (1-3 sentences)"}

TOOLS = [
    {"name": "get_overview", "description": "Desk snapshot: data mode, environment, broker mode, account equity, latest completed session, the watchlist screen (status and failing rules per symbol), the active rule set, risk limits and kill switch, and the day-trading session. Call this first in a conversation.",
     "input_schema": _obj({}, [])},
    {"name": "get_symbol_evaluation", "description": "Full rule-by-rule evidence for one symbol under the active rules: each rule's threshold, observed value and result, the facts used, the suggested stop, entry assumption, holding plan and earnings warning.",
     "input_schema": _obj({"symbol": SYM}, ["symbol"])},
    {"name": "get_indicators", "description": "Latest daily indicator values for a symbol: close, EMA 9/20/50, SMA 50/200, RSI 2/14, ATR 14 (and as % of price), MACD 12/26/9, Bollinger 20/2 with %B, 20-day high/low, 52-week high/low, average volume and the last 5 closes.",
     "input_schema": _obj({"symbol": SYM}, ["symbol"])},
    {"name": "list_strategies", "description": "The strategy library (EMA pullback, breakout, mean reversion, trend following, earnings drift): what each looks for, its tunable parameters and defaults, and which strategy and parameters are active now.",
     "input_schema": _obj({}, [])},
    {"name": "get_positions_and_orders", "description": "Open positions and recent orders (simulator or broker) with status, fills, stop/target legs and the journal plan for each open trade.",
     "input_schema": _obj({}, [])},
    {"name": "get_journal", "description": "Recent journal entries: thesis, plan, outcome, P&L, exit reason and plan adherence.",
     "input_schema": _obj({"limit": {"type": "integer", "description": "How many entries (1-50)"}}, ["limit"])},
    {"name": "get_risk", "description": "Risk limits (versioned), kill switch state and the recent risk-override log.",
     "input_schema": _obj({}, [])},
    {"name": "get_forward_test", "description": "Live forward test for the active rule set: signals recorded, entries, closed results, win rate and returns versus the benchmark.",
     "input_schema": _obj({}, [])},
    {"name": "get_day_trading", "description": "Day-trading session (live or replay, cursor time), the intraday screen (VWAP reclaim / opening-range breakout evidence per symbol) and day-trade positions.",
     "input_schema": _obj({}, [])},
    {"name": "get_linked_accounts", "description": "Read-only snapshots of the user's linked accounts outside the trading broker (crypto exchanges such as Coinbase, Binance, Kraken, OKX, Bybit; Robinhood Crypto; moomoo/Futu; Phantom wallet): holdings, quantities, USD values where priced, and when each snapshot was taken.",
     "input_schema": _obj({}, [])},
    {"name": "get_crypto_orders", "description": "Crypto orders sent to linked exchange test/demo accounts: status, fills, average price and the protective stop for each, plus which exchange accounts can trade.",
     "input_schema": _obj({}, [])},
    {"name": "propose_crypto_order", "description": "Draft a spot crypto order on a linked exchange TEST/DEMO account (account ids from get_crypto_orders). Runs the live price, exchange minimums, balance and safety checks. The user confirms it in the Crypto tab.",
     "input_schema": _obj({"account_id": {"type": "string"}, "symbol": {"type": "string", "description": "BASE/QUOTE, e.g. BTC/USDT"},
                           "side": {"type": "string", "enum": ["buy", "sell"]},
                           "notional": {"type": "number", "description": "Order size in the quote currency, e.g. 500 (USDT)"},
                           "stop_price": {"type": ["number", "null"], "description": "Protective stop for a buy; null for none"},
                           "rationale": WHY}, ["account_id", "symbol", "side", "notional", "stop_price", "rationale"])},
    {"name": "propose_rule_change", "description": "Draft a new version of the swing rule set (strategy and/or parameter changes). Creates a proposal the user can apply; it does not change anything by itself. Parameter names must come from list_strategies.",
     "input_schema": _obj({"strategy": {"type": "string", "description": "Strategy id, e.g. ema_pullback; the current one if unchanged"},
                           "changes": {"type": "array", "description": "Parameters to change",
                                       "items": _obj({"name": {"type": "string"}, "value": {"type": ["number", "string"]}}, ["name", "value"])},
                           "rationale": WHY}, ["strategy", "changes", "rationale"])},
    {"name": "propose_risk_limits", "description": "Draft a change to the risk limits (max_open_positions, max_portfolio_heat_pct, max_sector_pct, daily_loss_limit_pct, pdt_equity_min, restricted). Creates a proposal only.",
     "input_schema": _obj({"changes": {"type": "array", "items": _obj({"name": {"type": "string"}, "value": {"type": ["number", "string", "array"], "items": {"type": "string"}}}, ["name", "value"])},
                           "rationale": WHY}, ["changes", "rationale"])},
    {"name": "propose_swing_order", "description": "Draft a swing buy for a symbol that qualifies now. Runs the same sizing and pre-trade risk checks as the ticket and returns them. The user opens it in the ticket and confirms there.",
     "input_schema": _obj({"symbol": SYM, "qty": {"type": ["integer", "null"], "description": "Shares; null to use the rule-based size"},
                           "stop_price": {"type": ["number", "null"], "description": "Protective stop; null for the rule's suggested stop"},
                           "rationale": WHY}, ["symbol", "qty", "stop_price", "rationale"])},
    {"name": "propose_day_trade", "description": "Draft a day trade for a symbol that qualifies on the current intraday bar. Runs the day-trade sizing, routing and risk checks. The user confirms it in the Day trading tab.",
     "input_schema": _obj({"symbol": SYM, "rationale": WHY}, ["symbol", "rationale"])},
    {"name": "propose_exit", "description": "Draft closing a position, canceling a working order, or flattening a day trade (order id from get_positions_and_orders). Creates a proposal only.",
     "input_schema": _obj({"order_id": {"type": "string"}, "action": {"type": "string", "enum": ["close", "cancel", "flatten"]}, "rationale": WHY},
                          ["order_id", "action", "rationale"])},
]


# ---------- client ----------

def key_source() -> str | None:
    if credentials.get_secret(KEY_NAME):
        return "keychain"
    if db.TENANT.get() is None and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return "environment"     # the operator's own key: single-user local app only, never shared with portal users
    return None


def client():
    if CLIENT_FACTORY:
        return CLIENT_FACTORY()
    import anthropic
    k = credentials.get_secret(KEY_NAME)
    if k:
        return anthropic.Anthropic(api_key=k)
    if db.TENANT.get() is not None:
        raise CopilotError("Add your Anthropic API key in the Copilot tab.", 409)
    return anthropic.Anthropic()


def save_key(key: str) -> dict:
    key = key.strip()
    if not key:
        raise CopilotError("Paste an Anthropic API key.")
    import anthropic
    try:
        anthropic.Anthropic(api_key=key, max_retries=0, timeout=15).models.retrieve(MODEL)   # free call; checks the key
    except anthropic.AuthenticationError:
        raise CopilotError("Anthropic rejected that key.")
    except anthropic.APIError as exc:
        raise CopilotError(f"Couldn't check the key: {exc}"[:300], 502)
    credentials.set_secret(KEY_NAME, key)
    return status()


def delete_key() -> dict:
    credentials.set_secret(KEY_NAME, None)
    return status()


PLAN_CHOICES = ("free", "free_gpt", "free_gemini", "oss", "local", "openai", "gemini")
FREE_CLOUD = {"free_gpt": "openai", "free_gemini": "gemini"}     # free plans that run on the operator's OpenAI / Gemini key

# Other frontier models through their OpenAI-compatible chat endpoints, on the user's own key.
# Model names change often, so the picker lists what the key can actually use (GET /models) instead of a fixed list.
CLOUD = {
    "openai": {"name": "OpenAI GPT", "base_url": "https://api.openai.com/v1", "key": "openai_api_key", "billed": "OpenAI",
               "keys_at": "platform.openai.com → API keys"},
    "gemini": {"name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key": "gemini_api_key",
               "billed": "Google", "keys_at": "aistudio.google.com → Get API key"},
}
_SKIP = ("embedding", "tts", "whisper", "dall-e", "image", "audio", "realtime", "moderation", "transcribe", "search", "davinci",
         "babbage", "aqa", "veo", "imagen", "live", "computer-use", "codex", "sora")
CLOUD_TRANSPORT = None       # tests inject an httpx transport for the fake OpenAI / Gemini servers


def _cloud_key(provider: str) -> str | None:
    return credentials.get_secret(CLOUD[provider]["key"])


def cloud_models(provider: str, key: str | None = None) -> list[str]:
    """Chat models this key can use, newest names first (from the provider's own model list)."""
    import httpx
    if provider not in CLOUD:
        raise CopilotError(f"Unknown provider '{provider}'.")
    key = key or _cloud_key(provider)
    if not key:
        raise CopilotError(f"Add your {CLOUD[provider]['name']} API key first.", 409)
    try:
        with httpx.Client(transport=CLOUD_TRANSPORT, timeout=15) as c:
            r = c.get(CLOUD[provider]["base_url"] + "/models", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        raise CopilotError(f"Couldn't reach {CLOUD[provider]['billed']}: {exc}"[:200], 502)
    if r.status_code in (400, 401, 403):
        raise CopilotError(f"{CLOUD[provider]['billed']} rejected that API key.", 400)
    if r.status_code >= 400:
        raise CopilotError(f"{CLOUD[provider]['billed']} error {r.status_code}: {r.text[:160]}", 502)
    ids = [m.get("id", "").removeprefix("models/") for m in r.json().get("data", [])]
    want = (lambda i: i.startswith(("gpt-", "o1", "o3", "o4", "o5", "chatgpt-"))) if provider == "openai" else (lambda i: i.startswith("gemini"))
    import re

    def rank(i: str):
        m = re.search(r"(\d+(?:\.\d+)?)", i)
        return (0 if i.startswith(("gpt-", "gemini")) else 1, -(float(m.group(1)) if m else 0), len(i), i)
    return sorted({i for i in ids if i and want(i) and not any(x in i for x in _SKIP)}, key=rank)   # flagship, newest first


def save_cloud_key(provider: str, key: str) -> dict:
    key = (key or "").strip()
    if not key:
        raise CopilotError("Paste an API key.")
    models = cloud_models(provider, key)          # checks the key with a free call before saving it
    credentials.set_secret(CLOUD[provider]["key"], key)
    s = settings()
    if not s.get(f"{provider}_model") and models:
        s[f"{provider}_model"] = models[0]
        db.set_setting("copilot", s)
    return status()


def delete_cloud_key(provider: str) -> dict:
    if provider not in CLOUD:
        raise CopilotError(f"Unknown provider '{provider}'.")
    credentials.set_secret(CLOUD[provider]["key"], None)
    return status()


def settings() -> dict:
    s = {"model": MODEL, **DEFAULT_LOCAL, **(db.get_setting("copilot") or {})}
    if s["model"] not in PLAN_CHOICES and s["model"] not in CLAUDE_MODELS:
        s["model"] = MODEL
    if s["model"] in CLAUDE_MODELS and not key_source() and not CLIENT_FACTORY:
        from . import copilot_plans          # no key of their own: start people on whatever is free here
        cfg = copilot_plans.config()
        s["model"] = next((p for p in copilot_plans.ORDER if cfg[p]["enabled"]), s["model"])
    return s


def _is_loopback(url: str) -> bool:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1")


def save_settings(model: str, local_base_url: str | None = None, local_model: str | None = None,
                  openai_model: str | None = None, gemini_model: str | None = None, oss_model: str | None = None) -> dict:
    from urllib.parse import urlparse
    if model not in PLAN_CHOICES and model not in CLAUDE_MODELS:
        raise CopilotError(f"Unknown model '{model}'.")
    s = settings()
    s["model"] = model
    if oss_model is not None:
        from . import copilot_plans
        if oss_model not in copilot_plans.config()["oss"]["models"]:
            raise CopilotError(f"'{oss_model}' isn't one of the open models this server offers.")
        s["oss_model"] = oss_model
    for prov, val in (("openai", openai_model), ("gemini", gemini_model)):
        if val is not None:
            if not val.strip() or len(val) > 80:
                raise CopilotError(f"Pick a {CLOUD[prov]['name']} model.")
            s[f"{prov}_model"] = val.strip()
    if local_base_url is not None:
        u = local_base_url.strip().rstrip("/")
        if urlparse(u).scheme not in ("http", "https") or not urlparse(u).hostname:
            raise CopilotError("The local server URL must look like http://localhost:11434/v1")
        s["local_base_url"] = u
    if local_model is not None:
        if not local_model.strip():
            raise CopilotError("Enter the local model name, e.g. qwen3.5:9b")
        s["local_model"] = local_model.strip()
    db.set_setting("copilot", s)
    return status()


def local_models(base_url: str | None = None) -> dict:
    """Models the local OpenAI-compatible server (Ollama, LM Studio, llama.cpp, vLLM) reports."""
    import httpx
    url = (base_url or settings()["local_base_url"]).rstrip("/")
    try:
        with httpx.Client(transport=LOCAL_TRANSPORT, timeout=5) as c:
            r = c.get(f"{url}/models")
            r.raise_for_status()
            ids = sorted(m.get("id") for m in r.json().get("data", []) if m.get("id"))
        return {"reachable": True, "base_url": url, "models": ids, "loopback": _is_loopback(url)}
    except Exception as exc:
        return {"reachable": False, "base_url": url, "models": [], "loopback": _is_loopback(url),
                "error": f"{type(exc).__name__}: {exc}"[:200]}


def _oss_label(s: dict) -> str:
    from . import copilot_plans
    models = copilot_plans.config()["oss"]["models"]
    chosen = s.get("oss_model") if s.get("oss_model") in models else copilot_plans.config()["oss"]["model"]
    return copilot_plans.open_model_label(chosen)


def status() -> dict:
    src = key_source()
    k = credentials.get_secret(KEY_NAME)
    s = settings()
    claude_ok = bool(src) or bool(CLIENT_FACTORY)
    from . import copilot_plans
    plans = copilot_plans.summary()
    pl = {p["id"]: p for p in plans}
    cloud = {p: {"name": v["name"], "configured": bool(_cloud_key(p)), "key_hint": credentials.hint(_cloud_key(p)),
                 "model": s.get(f"{p}_model"), "billed": v["billed"], "keys_at": v["keys_at"]} for p, v in CLOUD.items()}
    label = (CLAUDE_MODELS[s["model"]]["name"] + " · your key" if s["model"] in CLAUDE_MODELS else
             f"{pl['oss']['name']} ({_oss_label(s)})" if s["model"] == "oss" else
             f"{pl[s['model']]['name']} ({pl[s['model']]['model']})" if s["model"] in pl else
             f"{CLOUD[s['model']]['name']}: {s.get(s['model'] + '_model') or 'pick a model'} · your key" if s["model"] in CLOUD else
             f"Local: {s['local_model']}")
    ready = {**{p: pl[p]["enabled"] for p in pl}, "local": True,
             "openai": cloud["openai"]["configured"] and bool(cloud["openai"]["model"]),
             "gemini": cloud["gemini"]["configured"] and bool(cloud["gemini"]["model"])}.get(s["model"], claude_ok)
    return {"configured": ready or claude_ok or any(p["enabled"] for p in plans), "ready": ready, "claude_configured": claude_ok, "source": src,
            "key_hint": credentials.hint(k) if k else None, "model": s["model"], "effort": EFFORT, "plans": plans,
            "model_label": label,
            "settings": s, "local_loopback": _is_loopback(s["local_base_url"]),
            "models": [{"id": k_, **v} for k_, v in CLAUDE_MODELS.items()], "cloud": cloud}


# ---------- read tools ----------

def _num(x, nd=2):
    return None if x is None else round(float(x), nd)


def _ev_summary(ev: dict) -> dict:
    return {"symbol": ev["symbol"], "status": ev["status"], "date": ev.get("date"),
            "failed": ev.get("failed"), "unknown": [r["id"] for r in ev["rules"] if r["result"] == "unknown"]}


def t_overview(c, _):
    from . import day, intraday, risk, trading
    from .brokers import factory
    out = {"now_et": intraday.now_et().strftime("%Y-%m-%d %H:%M %a"), "data_mode": db.data_mode(), "environment": db.environment(),
           "broker_mode": trading.broker_mode(c)}
    try:
        acct = trading.broker_account()
    except trading.TradeError as exc:
        acct, out["broker_error"] = None, str(exc)
    eq, bp = trading.sizing_account(c, acct)
    out["account"] = {"equity": _num(eq), "buying_power": _num(bp), "source": factory.LABELS.get(out["broker_mode"]) if acct else "local paper account"}
    p, rs = trading.current_params(c)
    out["rules"] = {"version": rs["version"], "strategy": p.strategy, "params": asdict(p)}
    if trading.as_of(c) is None:
        out["screen"] = "no market data loaded yet"
    else:
        out["latest_session"] = trading.as_of(c)
        sc = [trading.evaluate_symbol(c, s, p) for s in db.kv_get(c, "watchlist", [])]
        out["screen"] = [_ev_summary(e) for e in sc]
    out["risk"] = {"limits": risk.limits(c)["params"], "kill_switch": risk.kill_switch(c)}
    try:
        st = day.state()
        out["day_trading"] = {k: st[k] for k in ("mode", "date", "cursor", "market_open", "version")} | {"strategy": st["params"]["strategy"]}
    except Exception as exc:
        out["day_trading"] = f"unavailable: {exc}"
    return out


def t_evaluation(c, a):
    from . import trading
    ev = trading.evaluate_symbol(c, a["symbol"].upper().strip())
    return {k: ev.get(k) for k in ("symbol", "date", "status", "strategy_name", "rules", "failed", "facts", "entry_assumption",
                                   "suggested_stop", "plan", "earnings_warning", "data_timestamp")}


def t_indicators(c, a):
    from . import indicators as ind
    from . import trading
    s = trading.load_series(c, a["symbol"].upper().strip())
    i = len(s.close) - 1
    cl = s.close
    last = lambda xs: _num(xs[i]) if xs and xs[i] is not None else None   # noqa: E731
    macd, sig, hist = ind.macd(cl)
    mid, up, lo = ind.bollinger(cl)
    atr14 = s.atr(14)
    w20 = [x for x in cl[-20:] if x is not None]
    w252h = [x for x in s.high[-252:] if x is not None]
    w252l = [x for x in s.low[-252:] if x is not None]
    vol20 = [v for v in s.volume[-20:] if v is not None]
    pb = None if up[i] is None or up[i] == lo[i] else _num((cl[i] - lo[i]) / (up[i] - lo[i]), 3)
    return {"symbol": s.symbol, "date": s.dates[i], "source": s.source, "close": last(cl),
            "ema9": last(s.ema(9)), "ema20": last(s.ema(20)), "ema50": last(s.ema(50)), "sma50": last(s.sma(50)), "sma200": last(s.sma(200)),
            "rsi2": last(s.rsi(2)), "rsi14": last(s.rsi(14)), "atr14": last(atr14),
            "atr14_pct": None if atr14[i] is None or not cl[i] else _num(atr14[i] / cl[i] * 100),
            "macd": last(macd), "macd_signal": last(sig), "macd_hist": last(hist),
            "bollinger": {"mid": last(mid), "upper": last(up), "lower": last(lo), "pct_b": pb},
            "high_20d": _num(max(w20)) if w20 else None, "low_20d": _num(min(w20)) if w20 else None,
            "high_52w": _num(max(w252h)) if w252h else None, "low_52w": _num(min(w252l)) if w252l else None,
            "avg_volume_20d": int(sum(vol20) / len(vol20)) if vol20 else None,
            "last_5_closes": [[d, _num(x)] for d, x in zip(s.dates[-5:], cl[-5:])],
            # precomputed so smaller models don't have to do arithmetic: close vs each average, in $ and %
            "close_vs": {name: None if v is None or cl[i] is None else {"diff": _num(cl[i] - v), "pct": _num((cl[i] - v) / v * 100)}
                         for name, v in (("ema20", s.ema(20)[i]), ("sma50", s.sma(50)[i]), ("sma200", s.sma(200)[i]))}}


def t_strategies(c, _):
    from . import trading
    from .rules import STRATEGIES
    p, rs = trading.current_params(c)
    return {"active": {"version": rs["version"], "strategy": p.strategy, "params": asdict(p)},
            "library": {k: {"name": v["name"], "style": v["style"], "looks_for": v["template"], "params": v["params"], "defaults": v["defaults"]}
                        for k, v in STRATEGIES.items()}}


def t_positions(c, _):
    from . import trading
    from .brokers import simulator as sim
    rows = []
    for r in c.execute("SELECT id FROM orders WHERE purpose='entry' ORDER BY created_at DESC LIMIT 25"):
        o = trading.order_view(c, r["id"])
        held = sim._open_qty(c, o["id"])
        j = c.execute("""SELECT j.plan_json, jo.status, jo.realized_pnl, jo.exit_reason FROM journal j
                         JOIN journal_outcomes jo ON jo.journal_id=j.id WHERE j.order_id=?""", (o["id"],)).fetchone()
        rows.append({"order_id": o["id"], "symbol": o["symbol"], "style": o.get("style"), "broker": o["broker"], "status": o["status"],
                     "qty": o["qty"], "filled_qty": o["filled_qty"], "avg_fill_price": o["avg_fill_price"], "held_now": held,
                     "stop_price": o["stop_price"], "target_price": o.get("target_price"), "created_at": o["created_at"],
                     "legs": [{k: ch[k] for k in ("purpose", "status", "stop_price")} for ch in o["children"]],
                     "plan": json.loads(j["plan_json"]) if j else None,
                     "outcome": {"status": j["status"], "realized_pnl": j["realized_pnl"], "exit_reason": j["exit_reason"]} if j else None})
    return {"orders": rows}


def t_journal(c, a):
    from . import trading
    n = max(1, min(int(a.get("limit") or 10), 50))
    out = []
    for e in trading.journal(c)[:n]:
        out.append({k: e.get(k) for k in ("id", "symbol", "created_at", "thesis", "plan", "status", "entry_date", "entry_price", "exit_date",
                                           "exit_price", "exit_reason", "realized_pnl", "return_pct", "adherence", "review_note")})
    return {"entries": out}


def t_risk(c, _):
    from . import risk
    return {"limits": risk.limits(c), "kill_switch": risk.kill_switch(c), "recent_overrides": risk.override_log(c, 10)}


def t_forward(c, _):
    from . import forward, trading
    _, rs = trading.current_params(c)
    s = forward.summary(c, rs["version"])
    return {"ruleset_version": rs["version"], "stats": s.get("stats"), "recent": [
        {k: r.get(k) for k in ("symbol", "signal_date", "state", "entry_price", "exit_price", "return_pct", "bench_return_pct")}
        for r in (s.get("signals") or s.get("rows") or [])[:15]]}


def t_day(c, _):
    from . import day
    st = day.state()
    try:
        sc = day.screen()
        res = [{"symbol": r["symbol"], "status": r["status"], "failed": r.get("failed"),
                "rules": [{k: x.get(k) for k in ("id", "name", "observed", "threshold", "result")} for x in r["rules"]],
                "entry": r.get("entry_assumption"), "stop": r.get("suggested_stop"), "target": r.get("target")} for r in sc["results"]]
    except Exception as exc:
        res = f"unavailable: {exc}"
    return {"session": {k: st[k] for k in ("mode", "date", "cursor", "market_open", "feed", "version")}, "rules": st["params"],
            "screen": res, "positions": day.positions()}


def t_crypto(c, _):
    from . import crypto_trading
    keep = ("id", "symbol", "side", "order_type", "purpose", "amount", "status", "filled", "average", "cost", "stop_price", "environment",
            "account_id", "created_at", "reason")
    return {"accounts": crypto_trading.tradable_accounts(),
            "orders": [{**{k: o.get(k) for k in keep}, "children": [{k: ch.get(k) for k in keep} for ch in o["children"]]}
                       for o in crypto_trading.list_orders(25)]}


def p_crypto(c, a, thread_id):
    from . import crypto_trading
    try:
        pv = crypto_trading.preview(a["account_id"], a["symbol"], a["side"], notional=a["notional"], stop_price=a.get("stop_price"))
    except crypto_trading.CryptoError as exc:
        raise CopilotError(str(exc))
    payload = {k: pv[k] for k in ("account_id", "account_label", "symbol", "side", "amount", "price", "est_cost", "stop_price",
                                  "est_loss_if_stopped", "blocked", "environment")}
    payload["checks"] = [x for x in pv["checks"] if x["result"] != "pass"]
    payload["form"] = {"account_id": a["account_id"], "symbol": pv["symbol"], "side": a["side"], "notional": a["notional"],
                       "stop_price": a.get("stop_price")}
    return _proposal(c, thread_id, "crypto_order", f"{a['side'].title()} {pv['amount']:g} {pv['base']} on {pv['account_label']}",
                     a["rationale"], payload)


def t_linked(c, _):
    from . import linked
    return linked.summary()


# ---------- propose tools ----------

def _proposal(c, thread_id, kind, title, rationale, payload) -> dict:
    pid = "cp_" + uuid.uuid4().hex[:12]
    c.execute("INSERT INTO copilot_proposals(id,thread_id,kind,title,rationale,payload_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
              (pid, thread_id, kind, title, rationale, json.dumps(payload), "proposed", _now()))
    return {"proposal_id": pid, "status": "proposed: waiting for the user to review it in the app (nothing has changed yet)", "title": title, **payload}


def p_rules(c, a, thread_id):
    from . import trading
    from .rules import STRATEGIES, RuleParams
    p, rs = trading.current_params(c)
    strat = a["strategy"] or p.strategy
    if strat not in STRATEGIES:
        raise CopilotError(f"Unknown strategy '{strat}'. Valid: {', '.join(STRATEGIES)}")
    base = asdict(p) if strat == p.strategy else asdict(RuleParams(strategy=strat, **STRATEGIES[strat]["defaults"]))
    fields = RuleParams.__dataclass_fields__
    changes = {}
    for ch in a["changes"]:
        n = ch["name"]
        if n not in fields or n == "strategy":
            raise CopilotError(f"'{n}' isn't a rule parameter.")
        typ = type(getattr(RuleParams(), n))
        try:
            changes[n] = typ(ch["value"]) if typ is not str else str(ch["value"])
        except (TypeError, ValueError):
            raise CopilotError(f"'{n}' needs a {typ.__name__}.")
    new = {**base, **changes, "strategy": strat}
    diff = {k: {"from": asdict(p).get(k) if strat == p.strategy else None, "to": v} for k, v in changes.items()}
    title = f"Rules v{(rs['version'] or 0) + 1}: {STRATEGIES[strat]['name']}" + (f", {', '.join(changes)}" if changes else "")
    return _proposal(c, thread_id, "rules", title, a["rationale"],
                     {"base_version": rs["version"], "strategy": strat, "params": new, "changes": diff})


def p_risk(c, a, thread_id):
    from . import risk
    cur = risk.limits(c)
    changes = {ch["name"]: ch["value"] for ch in a["changes"]}
    if isinstance(changes.get("restricted"), str):          # "AAPL, MSFT" from models that can only send text
        changes["restricted"] = [x for x in changes["restricted"].replace(";", ",").split(",") if x.strip()]
    bad = [k for k in changes if k not in risk.DEFAULTS]
    if bad:
        raise CopilotError(f"Not risk limits: {', '.join(bad)}. Valid: {', '.join(risk.DEFAULTS)}")
    try:
        new = risk.validate({**cur["params"], **changes})
    except ValueError as exc:
        raise CopilotError(str(exc))
    diff = {k: {"from": cur["params"].get(k), "to": new[k]} for k in changes}
    return _proposal(c, thread_id, "risk_limits", f"Risk limits v{cur['version'] + 1}: {', '.join(changes)}", a["rationale"],
                     {"base_version": cur["version"], "params": new, "changes": diff})


def _risk_brief(rk: dict) -> dict:
    return {"blocked": rk.get("blocked"), "needs_override": rk.get("needs_override"),
            "checks": [{k: x.get(k) for k in ("name", "result", "observed", "threshold")} for x in rk.get("checks", []) if x.get("result") != "pass"]}


def p_swing(c, a, thread_id, acct):
    from . import trading
    sym = a["symbol"].upper().strip()
    ov = {k: a[k] for k in ("qty", "stop_price") if a.get(k) is not None}
    try:
        pv = trading.make_preview(c, sym, ov, acct)
    except trading.TradeError as exc:
        raise CopilotError(str(exc))
    payload = {"symbol": sym, "qty": pv["qty"], "suggested_qty": pv["suggested_qty"], "entry_assumption": pv["entry_assumption"],
               "stop_price": pv["stop_price"], "est_loss_if_stopped": pv["est_loss_if_stopped"], "pct_of_equity": pv.get("pct_of_equity"),
               "plan": pv["plan"], "mode": pv["mode"], "ruleset_version": pv["ruleset_version"], "earnings_warning": pv.get("earnings_warning"),
               "risk": _risk_brief(pv["risk"]), "overrides": ov}
    return _proposal(c, thread_id, "swing_order", f"Buy {pv['qty']} {sym}, stop {pv['stop_price']}", a["rationale"], payload)


def p_day(c, a, thread_id):
    from . import day
    sym = a["symbol"].upper().strip()
    try:
        pv = day.preview(sym)
    except day.DayError as exc:
        raise CopilotError(str(exc))
    payload = {"symbol": sym, "qty": pv["qty"], "entry_assumption": pv["entry_assumption"], "stop_price": pv["stop_price"],
               "target": pv["target"], "flat_by": pv["flat_by"], "route": pv.get("route"), "cursor": pv["cursor"],
               "est_loss_if_stopped": pv["est_loss_if_stopped"], "risk": _risk_brief(pv["risk"])}
    return _proposal(c, thread_id, "day_trade", f"Day trade {pv['qty']} {sym} ({pv['cursor'][11:]} bar)", a["rationale"], payload)


def p_exit(c, a, thread_id):
    from .brokers import simulator as sim
    from .brokers.base import OPEN_STATUSES
    o = c.execute("SELECT * FROM orders WHERE id=?", (a["order_id"],)).fetchone()
    if not o or o["purpose"] != "entry":
        raise CopilotError("Unknown entry order id; get it from get_positions_and_orders.")
    held = sim._open_qty(c, o["id"])
    act = a["action"]
    if act == "cancel" and o["status"] not in OPEN_STATUSES:
        raise CopilotError(f"That order is {o['status']}; only working orders can be canceled.")
    if act in ("close", "flatten") and held <= 0:
        raise CopilotError("No open position for that order.")
    if act == "flatten" and o["style"] != "intraday":
        raise CopilotError("Flatten is for day trades; use close for swing positions.")
    verb = {"close": f"Close {held}", "cancel": "Cancel order for", "flatten": f"Flatten {held}"}[act]
    return _proposal(c, thread_id, "exit", f"{verb} {o['symbol']}", a["rationale"],
                     {"order_id": o["id"], "action": act, "symbol": o["symbol"], "held": held, "broker": o["broker"], "style": o["style"]})


READ = {"get_overview": t_overview, "get_symbol_evaluation": t_evaluation, "get_indicators": t_indicators, "list_strategies": t_strategies,
        "get_positions_and_orders": t_positions, "get_journal": t_journal, "get_risk": t_risk, "get_forward_test": t_forward,
        "get_day_trading": t_day, "get_linked_accounts": t_linked, "get_crypto_orders": t_crypto}
WRITE = {"propose_crypto_order", "propose_rule_change", "propose_risk_limits", "propose_swing_order", "propose_day_trade", "propose_exit"}
assert {t["name"] for t in TOOLS} == set(READ) | WRITE


def run_tool(name: str, args: dict, thread_id: str) -> tuple[dict, dict | None]:
    """Execute one tool. Returns (result, proposal-or-None). Errors come back as {'error': ...}."""
    from . import trading
    try:
        if name in READ:
            c = db.connect()
            try:
                return READ[name](c, args), None
            finally:
                c.close()
        if name == "propose_swing_order":
            acct = trading.broker_account()          # network read before the write transaction
            with db.tx() as c:
                r = p_swing(c, args, thread_id, acct)
            return r, r
        if name == "__bad_args__":
            return {"error": "Tool arguments were not valid JSON; call the tool again with JSON arguments."}, None
        if name == "propose_crypto_order":           # the preview makes network calls; keep them outside any transaction
            c = db.connect()
            try:
                c.execute("BEGIN")
                r = p_crypto(c, args, thread_id)
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
            finally:
                c.close()
            return r, r
        fn = {"propose_rule_change": p_rules, "propose_risk_limits": p_risk, "propose_day_trade": p_day, "propose_exit": p_exit}.get(name)
        if not fn:
            return {"error": f"unknown tool {name}"}, None
        with db.tx() as c:
            r = fn(c, args, thread_id)
        return r, r
    except (CopilotError, trading.TradeError) as exc:
        return {"error": str(exc)}, None
    except Exception as exc:                                   # surface, don't crash the chat
        return {"error": f"{type(exc).__name__}: {exc}"[:400]}, None


# ---------- conversation ----------

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(block) -> dict:
    return block.model_dump(mode="json", exclude_none=True) if hasattr(block, "model_dump") else dict(block)


def _load(thread_id: str | None) -> tuple[str, list, str | None]:
    if thread_id:
        c = db.connect()
        try:
            r = c.execute("SELECT messages_json, model FROM copilot_threads WHERE id=?", (thread_id,)).fetchone()
        finally:
            c.close()
        if not r:
            raise CopilotError("Unknown conversation.", 404)
        return thread_id, json.loads(r["messages_json"]), r["model"]
    return "ct_" + uuid.uuid4().hex[:12], [], None


def _save(thread_id: str, messages: list, title: str, model: str) -> None:
    with db.tx() as c:
        c.execute("""INSERT INTO copilot_threads(id,title,messages_json,created_at,updated_at,model) VALUES(?,?,?,?,?,?)
                     ON CONFLICT(id) DO UPDATE SET messages_json=excluded.messages_json, updated_at=excluded.updated_at,
                     model=excluded.model""",
                  (thread_id, title[:80], json.dumps(messages), _now(), _now(), model))


def _family(model: str | None) -> str | None:
    """Conversation format: Claude's, or the OpenAI chat format shared by GPT, Gemini, open-source and local models."""
    return None if not model else ("local" if model.startswith(("local:", "oss:", "openai:", "gemini:", "free_gpt:", "free_gemini:")) else "claude")


def chat(text: str, thread_id: str | None = None, level: str = "beginner", model: str | None = None) -> dict:
    """One user turn: run the tool loop until the model answers. Returns the reply, the tool steps and any proposals."""
    text = (text or "").strip()
    if not text:
        raise CopilotError("Type a question.")
    s = settings()
    choice = model or s["model"]
    if choice not in PLAN_CHOICES and choice not in CLAUDE_MODELS:
        raise CopilotError(f"Unknown model '{choice}'.")
    from . import copilot_plans
    cfg = copilot_plans.config()
    if choice in copilot_plans.PLANS:
        copilot_plans.check(choice)
    if choice in CLOUD:
        if not _cloud_key(choice):
            raise CopilotError(f"Add your {CLOUD[choice]['name']} API key in the Copilot tab first.", 409)
        if not s.get(f"{choice}_model"):
            raise CopilotError(f"Pick a {CLOUD[choice]['name']} model in the Copilot tab.", 409)
    oss_model = s.get("oss_model") if s.get("oss_model") in cfg["oss"]["models"] else cfg["oss"]["model"]
    model_id = {"local": f"local:{s['local_model']}", "oss": f"oss:{oss_model}",
                "free": f"free:{cfg['free']['model']}", "openai": f"openai:{s.get('openai_model')}",
                **{p: f"{p}:{copilot_plans.model_for(p)}" for p in FREE_CLOUD if p == choice},
                "gemini": f"gemini:{s.get('gemini_model')}"}.get(choice, choice)
    thread_id, messages, prev = _load(thread_id)
    if prev and _family(prev) != _family(model_id):
        raise CopilotError("This conversation used a different kind of model. Start a new conversation to switch "
                           "between Claude and GPT, Gemini or a local model.", 409)
    if choice in CLAUDE_MODELS and not status()["claude_configured"]:
        raise CopilotError("Add your Anthropic API key in the Copilot tab, or pick the free or open-source plan.", 409)
    from . import intraday
    stamp = intraday.now_et().strftime("%Y-%m-%d %H:%M ET")
    messages.append({"role": "user", "content": f"[{stamp} · reader level: {level}]\n{text}"})
    if choice == "local":
        out = _local_loop(messages, thread_id, s)
    elif choice == "oss":
        # Ollama on this machine, or a hosted open-model service (Groq, OpenRouter, Together...) with SWINGDESK_OSS_API_KEY.
        out = _local_loop(messages, thread_id, {"local_base_url": cfg["oss"]["base_url"], "local_model": oss_model},
                          api_key=server_settings.get("SWINGDESK_OSS_API_KEY") or None)
    elif choice in CLOUD:
        out = _local_loop(messages, thread_id, {"local_base_url": CLOUD[choice]["base_url"], "local_model": s[f"{choice}_model"]},
                          api_key=_cloud_key(choice), provider=choice)
    elif choice in FREE_CLOUD:
        prov = FREE_CLOUD[choice]
        out = _local_loop(messages, thread_id, {"local_base_url": CLOUD[prov]["base_url"], "local_model": copilot_plans.model_for(choice)},
                          api_key=copilot_plans.operator_key(choice), provider=prov)
    elif choice == "free":
        out = _claude_loop(messages, thread_id, cfg["free"]["model"], operator=True)
    else:
        out = _claude_loop(messages, thread_id, choice)
    if choice in copilot_plans.PLANS:
        copilot_plans.record(choice, out["usage"])
    _save(thread_id, messages, text, model_id)
    return {"thread_id": thread_id, "model": model_id, **out}


def _run_calls(calls: list[tuple[str, str, dict]], thread_id: str, steps: list, proposals: list) -> list[tuple[str, str, bool]]:
    """Run (call_id, name, args) tool calls; returns (call_id, json result, is_error)."""
    out = []
    for cid, name, args in calls:
        res, prop = run_tool(name, args, thread_id)
        steps.append({"tool": name, "input": args, "ok": "error" not in res, "error": res.get("error")})
        if prop:
            proposals.append(get_proposal(prop["proposal_id"]))
        body = json.dumps(res, default=str)
        if len(body) > 30000:
            body = body[:30000] + ' …"(truncated)"'
        out.append((cid, body, "error" in res))
    return out


def operator_client():
    """The server operator's own key, used only for the metered free plan."""
    if CLIENT_FACTORY:
        return CLIENT_FACTORY()
    import anthropic
    from . import copilot_plans
    return anthropic.Anthropic(api_key=copilot_plans.operator_key("free"))


def _claude_loop(messages: list, thread_id: str, model: str, operator: bool = False) -> dict:
    import anthropic
    cl = operator_client() if operator else client()
    steps, proposals, reply, usage = [], [], "", {"input": 0, "output": 0, "cache_read": 0}
    extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if CLAUDE_MODELS.get(model, {}).get("fallbacks", True) else {}
    for _ in range(MAX_STEPS):
        try:
            resp = cl.beta.messages.create(
                model=model, max_tokens=16000, system=SYSTEM, tools=TOOLS, messages=messages,
                thinking={"type": "adaptive"}, output_config={"effort": EFFORT},
                cache_control={"type": "ephemeral"}, **extra,
            )
        except anthropic.AuthenticationError:
            raise CopilotError("Anthropic rejected the API key. Replace it in the Copilot tab.", 401)
        except anthropic.RateLimitError:
            raise CopilotError("Anthropic rate limit hit; try again in a minute.", 429)
        except anthropic.APIConnectionError:
            raise CopilotError("Couldn't reach Anthropic. Check the internet connection.", 502)
        except anthropic.APIStatusError as exc:
            raise CopilotError(f"Anthropic error {exc.status_code}: {exc.message}"[:300], 502)
        u = getattr(resp, "usage", None)
        if u:
            usage["input"] += getattr(u, "input_tokens", 0) or 0
            usage["output"] += getattr(u, "output_tokens", 0) or 0
            usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        messages.append({"role": "assistant", "content": [_dump(b) for b in resp.content]})
        if resp.stop_reason == "refusal":
            reply = "Claude declined to answer this one. Try rephrasing the question."
            break
        if resp.stop_reason == "pause_turn":
            continue
        uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not uses:
            reply = "".join(b.text for b in resp.content if b.type == "text").strip()
            if resp.stop_reason == "max_tokens":
                reply += "\n\n(Reply cut off at the length limit.)"
            break
        calls = [(b.id, b.name, b.input if isinstance(b.input, dict) else json.loads(b.input or "{}")) for b in uses]
        results = [{"type": "tool_result", "tool_use_id": cid, "content": body, **({"is_error": True} if err else {})}
                   for cid, body, err in _run_calls(calls, thread_id, steps, proposals)]
        messages.append({"role": "user", "content": results})      # all results of one turn in one message
    else:
        reply = "I stopped after too many lookups. Ask a narrower question."
    return {"reply": reply, "steps": steps, "proposals": proposals, "usage": usage}


# ---------- local models (OpenAI-compatible chat completions: Ollama, LM Studio, llama.cpp, vLLM) ----------

OPENAI_TOOLS = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
                for t in TOOLS]
LOCAL_HINT = ("\n\nYou are running as a small local model. Call one tool at a time, use exact tool names, pass arguments as JSON "
              "matching the schema, and keep the final answer short.")


def _strip_think(t: str | None) -> str:
    import re
    return re.sub(r"<think>.*?</think>", "", t or "", flags=re.S).strip()


def _gemini_schema(sch):
    """Gemini's function declarations take an OpenAPI subset: no additionalProperties, and one type plus nullable
    instead of a list of types."""
    if isinstance(sch, list):
        return [_gemini_schema(x) for x in sch]
    if not isinstance(sch, dict):
        return sch
    out = {}
    for k, v in sch.items():
        if k == "additionalProperties":
            continue
        if k == "type" and isinstance(v, list):
            types = [t for t in v if t != "null"]
            out["type"] = types[0] if len(types) == 1 else "string"
            if "null" in v:
                out["nullable"] = True
            continue
        out[k] = _gemini_schema(v)
    if out.get("type") == "string" and isinstance(sch.get("type"), list):
        out.pop("items", None)              # a multi-type field became a plain string for Gemini
    return out


GEMINI_TOOLS = [{"type": "function", "function": {**t["function"], "parameters": _gemini_schema(t["function"]["parameters"])}}
                for t in OPENAI_TOOLS]


def _local_loop(messages: list, thread_id: str, s: dict, api_key: str | None = None, provider: str | None = None) -> dict:
    import httpx
    url = s["local_base_url"].rstrip("/") + "/chat/completions"
    steps, proposals, reply, usage = [], [], "", {"input": 0, "output": 0, "cache_read": 0}
    sys_msg = {"role": "system", "content": SYSTEM + ("" if provider else LOCAL_HINT)}
    who = CLOUD[provider]["billed"] if provider else "The local model"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    tools = GEMINI_TOOLS if provider == "gemini" else OPENAI_TOOLS
    transport = CLOUD_TRANSPORT if provider else LOCAL_TRANSPORT
    with httpx.Client(transport=transport, timeout=httpx.Timeout(600, connect=10)) as http:
        for _ in range(MAX_STEPS):
            try:
                r = http.post(url, headers=headers, json={"model": s["local_model"], "messages": [sys_msg, *messages], "tools": tools, "stream": False})
            except httpx.ConnectError:
                if provider:
                    raise CopilotError(f"Couldn't reach {who}. Check the internet connection.", 502)
                raise CopilotError(f"No local model server at {s['local_base_url']}. Start Ollama (or LM Studio) and try again.", 502)
            except httpx.TimeoutException:
                raise CopilotError(f"{who} took too long to answer.", 504)
            if provider and r.status_code in (401, 403):
                raise CopilotError(f"{who} rejected the API key. Replace it in the Copilot tab.", 401)
            if provider and r.status_code == 429:
                raise CopilotError(f"{who} rate limit or quota reached; try again later or check your plan with {who}.", 429)
            if not provider and r.status_code in (401, 403):
                raise CopilotError("The open-source model service rejected the server's key (SWINGDESK_OSS_API_KEY).", 502)
            if r.status_code == 404:
                if provider:
                    raise CopilotError(f"{who} doesn't offer '{s['local_model']}' to this key. Pick another model.", 502)
                raise CopilotError(f"The local server doesn't have '{s['local_model']}'. Pull it first (e.g. ollama pull {s['local_model']}).", 502)
            if r.status_code >= 400:
                raise CopilotError(f"{who if provider else 'Local model server'} error {r.status_code}: {r.text[:200]}", 502)
            data = r.json()
            u = data.get("usage") or {}
            usage["input"] += u.get("prompt_tokens", 0) or 0
            usage["output"] += u.get("completion_tokens", 0) or 0
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            calls = msg.get("tool_calls") or []
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             **({"tool_calls": calls} if calls else {})})
            if not calls:
                reply = _strip_think(msg.get("content")) or f"({who} returned an empty answer.)"
                break
            parsed = []
            for i, tc in enumerate(calls):
                fn = tc.get("function") or {}
                raw = fn.get("arguments") or "{}"
                try:
                    args = raw if isinstance(raw, dict) else json.loads(raw)
                except json.JSONDecodeError:
                    args = {"__invalid_json__": str(raw)[:200]}
                parsed.append((tc.get("id") or f"call_{i}", fn.get("name") or "", args))
            for cid, body, _err in _run_calls(
                    [(cid, n, a) if "__invalid_json__" not in a else (cid, "__bad_args__", a) for cid, n, a in parsed],
                    thread_id, steps, proposals):
                messages.append({"role": "tool", "tool_call_id": cid, "content": body})
        else:
            reply = "I stopped after too many lookups. Ask a narrower question."
    return {"reply": reply, "steps": steps, "proposals": proposals, "usage": usage}


def thread(thread_id: str) -> dict:
    _, messages, model = _load(thread_id)
    turns = []
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], str):
            turns.append({"role": "user", "text": m["content"].split("\n", 1)[-1]})
        elif m["role"] == "assistant" and isinstance(m["content"], list):
            t = "".join(b.get("text", "") for b in m["content"] if b.get("type") == "text").strip()
            if t and not any(b.get("type") == "tool_use" for b in m["content"]):
                turns.append({"role": "assistant", "text": t})
        elif m["role"] == "assistant" and not m.get("tool_calls"):
            t = _strip_think(m.get("content"))
            if t:
                turns.append({"role": "assistant", "text": t})
    c = db.connect()
    try:
        props = [_prop_row(r) for r in c.execute("SELECT * FROM copilot_proposals WHERE thread_id=? ORDER BY created_at", (thread_id,))]
    finally:
        c.close()
    return {"thread_id": thread_id, "model": model, "turns": turns, "proposals": props}


def threads() -> list[dict]:
    c = db.connect()
    try:
        return [dict(r) for r in c.execute("SELECT id,title,model,created_at,updated_at FROM copilot_threads ORDER BY updated_at DESC LIMIT 20")]
    finally:
        c.close()


# ---------- proposals ----------

def _prop_row(r) -> dict:
    d = dict(r)
    d["payload"] = json.loads(d.pop("payload_json"))
    return d


def get_proposal(pid: str) -> dict:
    c = db.connect()
    try:
        r = c.execute("SELECT * FROM copilot_proposals WHERE id=?", (pid,)).fetchone()
    finally:
        c.close()
    if not r:
        raise CopilotError("Unknown proposal.", 404)
    return _prop_row(r)


def _decide(pid: str, status_: str, result: str) -> dict:
    with db.tx() as c:
        c.execute("UPDATE copilot_proposals SET status=?, result=?, decided_at=? WHERE id=?", (status_, result[:500], _now(), pid))
    return get_proposal(pid)


def pending() -> list[dict]:
    """Proposals still waiting for the user, from any conversation, including AI apps connected over MCP."""
    c = db.connect()
    try:
        return [_prop_row(r) | {"source": "mcp" if r["thread_id"].startswith("mcp:") else "copilot"}
                for r in c.execute("SELECT * FROM copilot_proposals WHERE status='proposed' ORDER BY created_at DESC LIMIT 30")]
    finally:
        c.close()


def dismiss(pid: str) -> dict:
    p = get_proposal(pid)
    if p["status"] != "proposed":
        raise CopilotError(f"This proposal is already {p['status']}.", 409)
    return _decide(pid, "dismissed", "dismissed by the user")


def apply(pid: str) -> dict:
    """The user's click. Rules, limits and exits are applied here through the same service functions as the
    regular endpoints; orders are not: the client opens the ticket, where the user confirms as usual."""
    from . import broker_sync, day, risk, trading
    from .brokers import factory
    p = get_proposal(pid)
    if p["status"] != "proposed":
        raise CopilotError(f"This proposal is already {p['status']}.", 409)
    pl, kind = p["payload"], p["kind"]
    if kind in ("swing_order", "day_trade", "crypto_order"):
        return {"proposal": _decide(pid, "opened", "opened in the ticket for review"),
                "open": {"swing_order": "ticket", "day_trade": "day", "crypto_order": "crypto"}[kind], "symbol": pl["symbol"],
                "overrides": pl.get("overrides", {}), "form": pl.get("form")}
    try:
        if kind == "rules":
            with db.tx() as c:
                cur = trading.active_ruleset(c)
                if (cur["version"] if cur else None) != pl["base_version"]:
                    raise CopilotError("The rules changed since this proposal; ask the copilot again.", 409)
                rs = trading.confirm_ruleset(c, f"Copilot proposal {pid}: {p['rationale']}", pl["params"], [], "")
            msg = f"saved as rules v{rs['version']}"
        elif kind == "risk_limits":
            with db.tx() as c:
                if risk.limits(c)["version"] != pl["base_version"]:
                    raise CopilotError("The risk limits changed since this proposal; ask the copilot again.", 409)
                lim = risk.save_limits(c, pl["params"], f"copilot proposal {pid}: {p['rationale']}"[:200])
            msg = f"saved as risk limits v{lim['version']}"
        else:   # exit
            oid, act = pl["order_id"], pl["action"]
            if act == "cancel":
                trading.cancel_order(oid)
            elif act == "flatten":
                day.flatten(oid)
            elif factory.is_external(pl["broker"]):
                broker_sync.manual_close(oid)
            else:
                with db.tx() as c:
                    trading.close_position(c, oid)
            msg = {"cancel": "cancel sent", "flatten": "flatten sent", "close": "exit order sent"}[act]
    except CopilotError:
        raise
    except (trading.TradeError, day.DayError, ValueError) as exc:
        _decide(pid, "failed", str(exc))
        raise CopilotError(str(exc), getattr(exc, "code", 409))
    return {"proposal": _decide(pid, "applied", msg)}
