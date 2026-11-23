"""One view of every broker, exchange, wallet and AI option, with how each signs in and whether it's connected.

Sign-in methods, best first:
  oauth    "Sign in with <broker>": the broker's own login page; tokens refresh automatically. Needs the server operator
           to register an app with that broker (client id/secret in the environment); otherwise it's shown as unavailable.
  api_key  paste a key pair created in the broker's or exchange's site (stored encrypted / in the keychain).
  gateway  a broker program on your machine you log in to (IBKR Client Portal Gateway, moomoo/Futu OpenD).
  address  a public wallet address, watch-only.
"""
from __future__ import annotations

from . import copilot, credentials, db, linked, oauth
from .brokers import ibkr, registry

GROUPS = [("stocks", "Stock brokers"), ("crypto", "Crypto exchanges"), ("wallets", "Wallets"), ("ai", "AI models and apps")]
GROUP_OF = {"alpaca": "stocks", "ibkr": "stocks", "schwab": "stocks", "tradier": "stocks", "tradestation": "stocks", "etrade": "stocks",
            "snaptrade": "stocks", "moomoo": "stocks", "robinhood": "crypto", "webull": "stocks", "fidelity": "stocks", "coinbase": "crypto",
            "binance": "crypto", "kraken": "crypto", "okx": "crypto", "bybit": "crypto", "phantom": "wallets", "fix": "stocks"}


def _linked_by_provider() -> dict:
    out: dict[str, list] = {}
    for a in linked.list_accounts():
        out.setdefault(a["provider"], []).append({"id": a["id"], "label": a["label"], "sandbox": a["sandbox"],
                                                   "live": bool(a.get("live_enabled_at")), "total_usd": (a.get("snapshot") or {}).get("total_usd")})
    return out


def overview() -> dict:
    lk = _linked_by_provider()
    k, _, _ = credentials.get_alpaca("paper")
    kl, _, _ = credentials.get_alpaca("real")
    items = []
    for b in registry.catalogue():
        if b["id"] == "fix":
            continue
        methods = []
        if b["id"] in oauth.PROVIDERS:
            st = oauth.status(b["id"])
            methods.append({"kind": "oauth", "label": f"Sign in with {b['name'].split(' (')[0]}", "available": st["configured"],
                            "connected": st["connected"], "provider": b["id"],
                            "why": None if st["configured"] else "Not offered on this server yet (the operator registers an app with the broker)."})
        if b["id"] == "alpaca":
            methods.append({"kind": "api_key", "label": "Paste API keys (paper)", "available": True, "connected": bool(k), "target": "alpaca_paper"})
            methods.append({"kind": "api_key", "label": "Live keys (real money)", "available": True, "connected": bool(kl), "target": "alpaca_live"})
        if b["id"] == "ibkr":
            methods.append({"kind": "gateway", "label": "Log in at the IBKR gateway", "available": True,
                            "connected": bool(ibkr.configured()), "target": "ibkr"})
        prov = b.get("linked_provider")
        if prov:
            kind = linked.PROVIDERS[prov]["kind"]
            methods.append({"kind": "address" if kind == "solana" else "gateway" if kind == "futu" else "api_key",
                            "label": {"solana": "Watch a public address", "futu": "Connect through OpenD"}.get(kind, "Paste API key"),
                            "available": True, "connected": bool(lk.get(prov)), "target": prov, "accounts": lk.get(prov, [])})
            if prov == "binance":
                methods.append({"kind": "api_key", "label": "Binance.US API key", "available": True, "connected": bool(lk.get("binanceus")),
                                "target": "binanceus", "accounts": lk.get("binanceus", [])})
        connected = any(m.get("connected") for m in methods)
        needs_setup = any(m["kind"] == "oauth" and not m["available"] for m in methods)
        state = "connected" if connected else ("available" if any(m["available"] for m in methods) else
                                               "setup" if needs_setup else
                                               "unavailable" if b["id"] in ("fidelity", "webull") or b["status"] == "unavailable" else "planned")
        items.append({"id": b["id"], "name": b["name"], "group": GROUP_OF.get(b["id"], "stocks"), "segment": b["segment"], "assets": b["assets"],
                      "how": b["how"], "paper": b["paper"], "state": state, "methods": methods,
                      "trading": b["status"] in ("available",) or bool(prov and linked.PROVIDERS[prov]["kind"] in linked.TRADING_KINDS)})
    st = copilot.status()
    ai = [{"id": "copilot", "name": "Tickmark Copilot", "group": "ai", "state": "connected" if st["ready"] else "available",
           "segment": "Built-in assistant", "assets": st["model_label"], "how": "Pick a plan or model in the Copilot tab.",
           "methods": [*[{"kind": "plan", "label": p["name"], "available": p["enabled"], "connected": st["model"] == p["id"],
                          "detail": p["note"], "used": p["used"], "limit": p["limit"], "period": p["period"]} for p in st["plans"]],
                       {"kind": "api_key", "label": "Your Anthropic API key", "available": True, "connected": st["source"] == "keychain", "target": "anthropic"},
                       *[{"kind": "api_key", "label": f"Your {c['name']} API key", "available": True, "connected": c["configured"],
                          "target": "anthropic", "detail": f"billed by {c['billed']}" + (f" · {c['model']}" if c["model"] else "")}
                         for c in st["cloud"].values()],
                       {"kind": "local", "label": "Your own local model (Ollama, LM Studio)", "available": True, "connected": st["model"] == "local"}]},
          {"id": "connectors", "name": "Claude and other AI apps", "group": "ai", "state": "available", "segment": "Use Tickmark inside your AI app",
           "assets": "Your Claude (or other) plan pays for the AI; Tickmark adds the tools",
           "how": "In Claude, add a custom connector with this server's /mcp URL and sign in with your Tickmark account; "
                  "or create a personal token for Claude Code.",
           "methods": [{"kind": "connector", "label": "Add as a connector (sign in with OAuth)", "available": True, "connected": False},
                       {"kind": "token", "label": "Personal token (Claude Code, scripts)", "available": True, "connected": False}]}]
    return {"groups": [{"id": g, "name": n} for g, n in GROUPS], "items": items + ai, "data_mode": db.data_mode()}
