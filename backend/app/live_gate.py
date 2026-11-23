"""Per-account live switch for stocks (Alpaca live, Interactive Brokers live).

Real money is opt-in per account. Enabling needs:
  * Live data mode, risk limits deliberately saved (version >= 1), and the kill switch off
  * Alpaca: separate live API keys, tested and saved. IBKR: a live (non-DU) account logged in at the gateway
  * the user types CONFIRM_PHRASE
The paper track record (closed trades, plan adherence, last reconciliation) is still shown, but it is
information, not a requirement. Crypto exchange accounts have their own switch (app/linked.py). Real-money trades go to their own database
(swingdesk-real-money.db), every live order needs the ticker typed to confirm,
and switching back to paper is always one click.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import credentials, db

MIN_CLOSED = 20
MIN_FOLLOWED = 0.8
CONFIRM_PHRASE = "I understand this trades real money"


def enabled() -> bool:
    return db.environment() == "real" and bool(db.get_setting("live_enabled_at"))


def _paper_stats() -> dict:
    c = db.connect("live", env="paper")
    try:
        db.init_schema(c)
        rows = c.execute("SELECT adherence_json FROM journal_outcomes WHERE status='closed'").fetchall()
        followed = sum(1 for r in rows if r["adherence_json"] and json.loads(r["adherence_json"]).get("followed"))
        rec = db.kv_get(c, "reconciliation")
        lim = c.execute("SELECT MAX(version) FROM risk_limits").fetchone()[0] or 0
        ks = db.kv_get(c, "kill_switch", {"on": False})
        rs = c.execute("SELECT MAX(version) FROM rulesets").fetchone()[0]
    finally:
        c.close()
    return {"closed": len(rows), "followed": followed, "reconciliation": rec, "limits_version": lim,
            "kill_switch": ks.get("on", False), "ruleset_version": rs}


def status() -> dict:
    st = _paper_stats()
    rate = st["followed"] / st["closed"] if st["closed"] else 0.0
    rec = st["reconciliation"]
    checks = [
        {"name": "Risk limits deliberately set", "ok": st["limits_version"] >= 1, "detail": f"limits v{st['limits_version']}"},
        {"name": "Kill switch off", "ok": not st["kill_switch"], "detail": "on" if st["kill_switch"] else "off"},
        {"name": "Live data mode", "ok": db.data_mode() == "live", "detail": db.data_mode()},
    ]
    record = [   # shown, not required
        {"name": "Closed paper trades", "detail": f"{st['closed']} (suggested: {MIN_CLOSED}+)"},
        {"name": "Plan followed", "detail": f"{st['followed']} of {st['closed']} ({rate:.0%})"},
        {"name": "Last reconciliation", "detail": "never run" if not rec else ("clean" if rec.get("ok") else f"{len(rec.get('breaks', []))} break(s)")},
    ]
    eligible = all(c["ok"] for c in checks)
    k, _, src = credentials.get_alpaca("real")
    return {"eligible": eligible, "checks": checks, "paper_record": record, "enabled": enabled(), "environment": db.environment(),
            "live_keys": bool(k), "live_key_hint": credentials.hint(k), "live_key_source": src, "broker": db.get_setting("live_broker") or "alpaca",
            "confirm_phrase": CONFIRM_PHRASE, "enabled_at": db.get_setting("live_enabled_at")}


def enable(phrase: str, broker: str = "alpaca") -> dict:
    from .trading import TradeError
    st = status()
    if broker not in ("alpaca", "ibkr"):
        raise TradeError("broker must be alpaca or ibkr", 400)
    if not st["eligible"]:
        missing = [c["name"] for c in st["checks"] if not c["ok"]]
        raise TradeError("Can't enable real-money trading yet: " + "; ".join(missing), 409, {"gate": st})
    if broker == "alpaca" and not st["live_keys"]:
        raise TradeError("Connect your live Alpaca keys first.", 409, {"gate": st})
    if (phrase or "").strip() != CONFIRM_PHRASE:
        raise TradeError(f'Type exactly: "{CONFIRM_PHRASE}"', 422, {"gate": st})
    paper = db.connect("live", env="paper")
    try:
        carry = {k: db.kv_get(paper, k) for k in ("watchlist",)}
        rules = paper.execute("SELECT * FROM rulesets ORDER BY version DESC LIMIT 1").fetchone()
        limits = paper.execute("SELECT * FROM risk_limits ORDER BY version DESC LIMIT 1").fetchone()
    finally:
        paper.close()
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.set_setting("environment", "real")
    db.set_setting("live_enabled_at", at)
    db.set_setting("live_broker", broker)
    real = db.connect("live", env="real")
    try:
        db.init_schema(real)
        real.execute("BEGIN IMMEDIATE")
        for k, v in carry.items():
            if v is not None and db.kv_get(real, k) is None:
                db.kv_set(real, k, v)
        if db.kv_get(real, "account") is None:
            db.kv_set(real, "account", {"starting_equity": 0.0, "cash": 0.0})
        # alpaca_paper is the Alpaca adapter id; environment 'real' points it at the live URL. IBKR uses the live account at the gateway.
        db.kv_set(real, "broker_mode", "alpaca_paper" if broker == "alpaca" else "ibkr")
        if rules and not real.execute("SELECT 1 FROM rulesets").fetchone():
            real.execute("INSERT INTO rulesets(version,name,source_text,params_json,interpretations_json,confirmed_at) VALUES(?,?,?,?,?,?)",
                         (1, rules["name"] + " (copied from paper)", rules["source_text"], rules["params_json"],
                          rules["interpretations_json"], at))
        if limits and not real.execute("SELECT 1 FROM risk_limits").fetchone():
            real.execute("INSERT INTO risk_limits(version,params_json,note,created_at) VALUES(1,?,?,?)",
                         (limits["params_json"], "copied from paper at live enablement", at))
        real.execute("INSERT INTO risk_overrides(client_order_id,symbol,check_id,observed,threshold,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                     ("-", "-", "live_trading_enabled", json.dumps([c["detail"] for c in st["checks"]]), "gate", phrase, at))
        real.execute("COMMIT")
    finally:
        real.close()
    return status()


def disable() -> dict:
    db.set_setting("environment", "paper")
    db.set_setting("live_enabled_at", None)
    return status()
