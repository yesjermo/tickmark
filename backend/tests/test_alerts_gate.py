"""Alerts (feed, de-duplication, channels) and the paper-to-live gate. No network, no real notifications."""
import json
from datetime import datetime

import httpx
import pytest

from app import alerts, credentials, db, live_gate
from app.brokers import alpaca as alpaca_mod
from fake_alpaca import GOOD_KEY, GOOD_SECRET, FakeAlpaca
from test_live_forward import confirm, go_to, live, world  # noqa: F401  (fixtures)


def preview(c, sym="NVDA", **ov):
    r = c.post("/api/previews", json={"symbol": sym, "overrides": ov})
    assert r.status_code == 200, r.text
    return r.json()


# ---------- alerts ----------

def test_signal_fill_and_close_alerts_in_feed(confirmed):
    confirmed.get("/api/watchlist/screen")
    pv = preview(confirmed)
    confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True})
    for _ in range(7):
        confirmed.post("/api/sim/advance")
    feed = confirmed.get("/api/alerts").json()
    kinds = [a["kind"] for a in feed["alerts"]]
    assert "signal" in kinds and "fill" in kinds and "close" in kinds
    assert feed["unread"] >= 3
    nvda_signals = [a for a in feed["alerts"] if a["kind"] == "signal" and a["symbol"] == "NVDA"]
    assert len(nvda_signals) == 1                    # de-duplicated across repeated screens
    confirmed.post("/api/alerts/read")
    assert confirmed.get("/api/alerts").json()["unread"] == 0


def test_kill_switch_alert_is_warn(confirmed):
    confirmed.put("/api/risk/kill-switch", json={"on": True, "reason": "market chaos"})
    a = confirmed.get("/api/alerts").json()["alerts"][0]
    assert a["kind"] == "risk" and a["severity"] == "warn"


def test_slack_delivery_respects_min_severity(confirmed, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "SLACK_TRANSPORT", httpx.MockTransport(lambda r: sent.append(json.loads(r.content)) or httpx.Response(200)))
    assert confirmed.put("/api/alerts/secrets", json={"slack_webhook": "https://hooks.slack.com/services/T/B/x"}).status_code == 200
    confirmed.put("/api/alerts/settings", json={"slack": True, "min_severity": "warn"})
    confirmed.get("/api/watchlist/screen")              # info-level signal: not sent
    confirmed.put("/api/risk/kill-switch", json={"on": True, "reason": "stop for today"})   # warn: sent
    texts = [m["text"] for m in sent]
    assert any("Kill switch ON" in t for t in texts) and not any("New setup" in t for t in texts)
    st = confirmed.get("/api/alerts").json()["settings"]
    assert st["has_slack_webhook"] is True and "hooks.slack.com" not in json.dumps(st)   # secret never returned


def test_bad_slack_url_rejected(client):
    assert client.put("/api/alerts/secrets", json={"slack_webhook": "https://evil.example/hook"}).status_code == 400


def test_email_channel_uses_starttls_and_keychain_password(client, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["host"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): sent["tls"] = True
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, m): sent["subject"] = m["Subject"]

    monkeypatch.setattr(alerts, "SMTP_FACTORY", FakeSMTP)
    client.put("/api/alerts/settings", json={"smtp_host": "smtp.example.com", "smtp_port": 587, "smtp_user": "me@example.com",
                                            "email_to": "me@example.com"})
    client.put("/api/alerts/secrets", json={"smtp_password": "app-password"})
    assert client.post("/api/alerts/test", json={"channel": "email"}).status_code == 200
    assert sent["tls"] and sent["login"] == ("me@example.com", "app-password") and "Test alert" in sent["subject"]


def test_desktop_channel_builds_safe_osascript(monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "_runner", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(alerts.sys, "platform", "darwin")
    alerts.send("desktop", 'Filled "NVDA"', "118 @ 167.84")
    assert calls[0][0] == "osascript" and '\\"NVDA\\"' in calls[0][2]


def test_reconciliation_break_raises_critical_alert(confirmed):
    pv = preview(confirmed)
    oid = confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True}).json()["order"]["id"]
    confirmed.post("/api/sim/advance")
    stop = next(ch for ch in next(o for o in confirmed.get("/api/orders").json() if o["id"] == oid)["children"] if ch["purpose"] == "stop")
    confirmed.post(f"/api/orders/{stop['id']}/cancel")
    confirmed.post("/api/reconcile")
    a = next(a for a in confirmed.get("/api/alerts").json()["alerts"] if a["kind"] == "reconciliation")
    assert a["severity"] == "critical" and "unprotected" in a["body"]


# ---------- paper-to-live gate ----------

@pytest.fixture
def fake(monkeypatch):
    f = FakeAlpaca()
    hosts = []
    real_handle = f.handle

    def handle(req):
        hosts.append(req.url.host)
        return real_handle(req)

    monkeypatch.setattr(alpaca_mod, "TRANSPORT", httpx.MockTransport(handle))
    f.hosts = hosts
    return f


def make_eligible(c):
    """Seed the paper record: 20 closed, plan-following trades, saved limits, clean reconciliation."""
    c.put("/api/risk/limits", json={"params": {"max_open_positions": 5}, "note": "reviewed"})
    conn = db.connect("live", env="paper")
    for i in range(20):
        cur = conn.execute("""INSERT INTO journal(order_id,symbol,created_at,thesis,ruleset_version,ruleset_json,evidence_json,
                              data_timestamp,order_json,plan_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                           (f"seed{i}", "NVDA", "2026-01-01", "seed", 1, "{}", "{}", "-", "{}", "{}"))
        conn.execute("INSERT INTO journal_outcomes(journal_id,status,adherence_json,updated_at) VALUES(?, 'closed', ?, ?)",
                     (cur.lastrowid, json.dumps({"followed": True}), "2026-01-02"))
    db.kv_set(conn, "reconciliation", {"ok": True, "breaks": [], "at": "2026-11-20T00:00:00+00:00", "mode": "alpaca_paper", "checked": 0})
    conn.close()


def test_gate_starts_off_and_paper_record_is_information_only(live):
    st = live.get("/api/live-gate").json()
    assert st["enabled"] is False and st["eligible"] is False                     # risk limits not saved yet
    assert not any("closed paper trades" in c["name"] for c in st["checks"])
    assert any("Closed paper trades" in r["name"] for r in st["paper_record"])
    r = live.post("/api/live-gate/enable", json={"phrase": live_gate.CONFIRM_PHRASE})
    assert r.status_code == 409 and "Risk limits" in r.json()["detail"]
    live.put("/api/risk/limits", json={"params": {}, "note": "deliberate"})
    st = live.get("/api/live-gate").json()
    assert st["eligible"] is True                                                  # no paper trades needed
    r = live.post("/api/live-gate/enable", json={"phrase": live_gate.CONFIRM_PHRASE})
    assert r.status_code == 409 and "live Alpaca keys" in r.json()["detail"]     # but live keys are


def test_live_keys_can_be_added_before_enabling(live, fake):
    r = live.post("/api/live-gate/keys", json={"key_id": GOOD_KEY, "secret": GOOD_SECRET})
    assert r.status_code == 200 and credentials.get_alpaca("real")[0] == GOOD_KEY
    assert live.get("/api/live-gate").json()["enabled"] is False
    r = live.post("/api/live-gate/enable", json={"phrase": "yes"})
    assert r.status_code == 409


def test_adapter_never_uses_live_url_unless_enabled(live, fake):
    credentials.save_alpaca(GOOD_KEY, GOOD_SECRET, env="real")
    with pytest.raises(alpaca_mod.BrokerError):
        alpaca_mod.AlpacaPaperBroker(env="real")
    credentials.save_alpaca(GOOD_KEY, GOOD_SECRET)
    alpaca_mod.AlpacaPaperBroker().account()
    assert fake.hosts[-1] == "paper-api.alpaca.markets"


def test_full_gate_flow_and_live_order(live, fake, world):
    confirm(live)                      # paper rules; copied into the real-money journal at enablement
    make_eligible(live)
    st = live.get("/api/live-gate").json()
    assert st["eligible"] is True and st["live_keys"] is False
    r = live.post("/api/live-gate/keys", json={"key_id": GOOD_KEY, "secret": GOOD_SECRET})
    assert r.status_code == 200 and fake.hosts[-1] == "api.alpaca.markets" and GOOD_SECRET not in r.text
    assert live.post("/api/live-gate/enable", json={"phrase": "yes"}).status_code == 422
    r = live.post("/api/live-gate/enable", json={"phrase": live_gate.CONFIRM_PHRASE})
    assert r.status_code == 200 and r.json()["enabled"] is True
    s = live.get("/api/status").json()
    assert s["environment"] == "real" and s["live_trading"] is True and s["mode"] == "alpaca_paper"
    assert db.db_path().name == "swingdesk-real-money.db"
    assert live.put("/api/broker/mode", json={"mode": "simulator"}).status_code == 409
    pv = preview(live)
    assert pv["equity_source"] == "Alpaca account"
    assert live.post("/api/orders", json={"preview_id": pv["id"], "confirm": True}).status_code == 422   # ticker not typed
    r = live.post("/api/orders", json={"preview_id": pv["id"], "confirm": True, "live_confirm": "nvda"})
    assert r.status_code == 200 and fake.hosts[-1] == "api.alpaca.markets"
    live_oid = r.json()["order"]["id"]
    # The real-money journal holds the order; the paper journal never sees it.
    assert [j["order_id"] for j in live.get("/api/journal").json()] == [live_oid]
    live.post("/api/live-gate/disable")
    assert db.environment() == "paper" and db.db_path().name == "swingdesk-live.db"
    assert live_oid not in [j["order_id"] for j in live.get("/api/journal").json()]


def test_switching_to_demo_turns_real_money_off(live, fake, world):
    make_eligible(live)
    live.post("/api/live-gate/keys", json={"key_id": GOOD_KEY, "secret": GOOD_SECRET})
    live.post("/api/live-gate/enable", json={"phrase": live_gate.CONFIRM_PHRASE})
    live.put("/api/data/mode", json={"mode": "demo"})
    assert db.environment() == "paper" and live_gate.enabled() is False
    db.set_data_mode("live")


def test_broker_catalogue(client):
    cat = {b["id"]: b for b in client.get("/api/brokers").json()}
    assert cat["alpaca"]["status"] == "available"
    assert cat["ibkr"]["connect"] == "gateway" and cat["schwab"]["connect"] == "oauth2" and cat["fix"]["phase"] == 3


def test_unknown_api_route_is_404_not_web_page(client):
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
