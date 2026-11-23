"""Multi-user portal (sign-in, invites, isolation, encrypted secrets, CSRF) and the MCP server."""
import json

import pytest
from fastapi.testclient import TestClient

from app import db, portal


@pytest.fixture
def P(client, monkeypatch):
    """Portal mode on; returns a factory of fresh browser-like clients (own cookie jars)."""
    monkeypatch.setenv("SWINGDESK_PORTAL", "on")
    monkeypatch.setattr(portal, "_fail", {})
    from app.main import app

    def new():
        return TestClient(app, headers={"x-swingdesk": "1"})
    return new


def signup(c, email, invite=""):
    r = c.post("/api/auth/signup", json={"email": email, "password": "correct horse battery", "invite": invite})
    assert r.status_code == 200, r.text
    return r.json()["user"]


def test_signin_required_and_first_user_is_admin(P):
    a = P()
    assert a.get("/api/watchlist/screen").status_code == 401
    assert a.get("/api/auth/status").json() == {"portal": True, "user": None, "first_user": True, "guest": False, "open_signup": False,
                                                "setup_code_required": False}
    u = signup(a, "Owner@Example.com")
    assert u["role"] == "admin" and u["email"] == "owner@example.com"
    assert a.get("/api/auth/status").json()["user"]["id"] == u["id"]
    assert a.get("/api/rulesets").status_code == 200


def test_invite_only_and_isolation_between_users(P, monkeypatch):
    a, b, x = P(), P(), P()
    signup(a, "admin@example.com")
    r = x.post("/api/auth/signup", json={"email": "c@example.com", "password": "correct horse battery"})
    assert r.status_code == 403
    code = a.post("/api/portal/invites", json={"email": "b@example.com"}).json()["code"]
    assert x.post("/api/auth/signup", json={"email": "c@example.com", "password": "correct horse battery", "invite": code}).status_code == 403
    ub = signup(b, "b@example.com", code)
    assert ub["role"] == "user" and b.post("/api/portal/invites", json={}).status_code == 403
    assert b.post("/api/auth/signup", json={"email": "d@example.com", "password": "correct horse battery", "invite": code}).status_code == 403

    # A's copilot key and settings are invisible to B; data folders are separate
    from app import copilot
    monkeypatch.setattr(copilot, "CLIENT_FACTORY", None)
    a.put("/api/copilot/settings", json={"model": "claude-sonnet-5-5"})
    assert b.get("/api/copilot").json()["model"] == "claude-opus-5-5"
    a.put("/api/risk/kill-switch", json={"on": True, "reason": "A only, testing"})
    assert b.get("/api/risk").json()["kill_switch"]["on"] is False
    assert (db.DATA_DIR / "users" / ub["id"]).is_dir()


def test_secrets_are_encrypted_per_user_and_env_keys_are_not_shared(P, monkeypatch):
    a, b = P(), P()
    ua = signup(a, "admin@example.com")
    code = a.post("/api/portal/invites", json={}).json()["code"]
    signup(b, "b@example.com", code)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "operator-key")             # the operator's own key never serves portal users
    assert a.get("/api/copilot").json()["claude_configured"] is False
    from app import credentials
    with portal.as_user(ua["id"]):
        credentials.set_secret("linked:la_1:secret", "super-secret-value")
        assert credentials.get_secret("linked:la_1:secret") == "super-secret-value"
    raw = (db.DATA_DIR / "users" / ua["id"] / "secrets.enc").read_bytes()
    assert b"super-secret-value" not in raw
    ub = b.get("/api/auth/status").json()["user"]["id"]
    with portal.as_user(ub):
        assert credentials.get_secret("linked:la_1:secret") is None


def test_csrf_header_and_login_throttle(P):
    a = P()
    signup(a, "admin@example.com")
    raw = TestClient(a.app)
    raw.cookies = a.cookies
    assert raw.put("/api/risk/kill-switch", json={"on": True, "reason": "cross-site attempt"}).status_code == 403
    x = P()
    for _ in range(5):
        assert x.post("/api/auth/login", json={"email": "admin@example.com", "password": "wrong password!"}).status_code == 401
    assert x.post("/api/auth/login", json={"email": "admin@example.com", "password": "correct horse battery"}).status_code == 429
    a.post("/api/auth/logout")
    assert a.get("/api/rulesets").status_code == 401


def rpc(c, token, method, params=None, id_=1, origin=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    if origin:
        h["Origin"] = origin
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, **({"params": params} if params else {})}, headers=h)


def test_mcp_server_per_user_tokens_and_proposals(P):
    a, b = P(), P()
    signup(a, "admin@example.com")
    code = a.post("/api/portal/invites", json={}).json()["code"]
    signup(b, "b@example.com", code)
    d = a.get("/api/rulesets").json()["draft"]
    a.post("/api/rulesets", json={"text": d["source_text"], "params": d["params"], "interpretations": d["interpretations"]})
    tok = a.post("/api/mcp/tokens", json={"name": "Claude Code"}).json()["token"]
    assert tok.startswith("sdk_") and a.get("/api/mcp/tokens").json()["tokens"][0]["name"] == "Claude Code"
    m = P()
    assert rpc(m, None, "initialize").status_code == 401
    init = rpc(m, tok, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}).json()
    assert init["result"]["protocolVersion"] == "2025-06-18" and init["result"]["serverInfo"]["name"] == "tickmark"
    assert m.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers={"Authorization": f"Bearer {tok}"}).status_code == 202
    names = {t["name"] for t in rpc(m, tok, "tools/list").json()["result"]["tools"]}
    assert "get_overview" in names and not any("submit" in n or "kill" in n for n in names)
    ov = rpc(m, tok, "tools/call", {"name": "get_overview", "arguments": {}}).json()["result"]
    assert ov["isError"] is False and json.loads(ov["content"][0]["text"])["rules"]["version"] == 1
    pr = rpc(m, tok, "tools/call", {"name": "propose_risk_limits", "arguments": {"changes": [{"name": "max_open_positions", "value": 3}],
                                                                               "rationale": "From Claude Code."}}).json()["result"]
    assert pr["isError"] is False and "Copilot tab" in pr["content"][0]["text"]
    pend = a.get("/api/copilot/proposals").json()
    assert len(pend) == 1 and pend[0]["source"] == "mcp" and pend[0]["status"] == "proposed"
    assert b.get("/api/copilot/proposals").json() == []                  # other users never see it
    assert a.get("/api/risk").json()["limits"]["version"] == 0           # nothing applied by the AI app
    assert rpc(m, tok, "tools/list", origin="https://evil.example").status_code == 403
    tid = a.get("/api/mcp/tokens").json()["tokens"][0]["id"]
    a.delete(f"/api/mcp/tokens/{tid}")
    assert rpc(m, tok, "tools/list").status_code == 401
    assert rpc(m, tok, "no/such").status_code == 401


def test_mcp_in_single_user_app(client):
    tok = client.post("/api/mcp/tokens", json={"name": "Claude Desktop"}).json()["token"]
    r = rpc(client, tok, "tools/list")
    assert r.status_code == 200 and len(r.json()["result"]["tools"]) >= 14
    assert rpc(client, tok, "bogus/method").json()["error"]["code"] == -32601


def test_setup_code_guards_the_first_admin_and_health_is_public(P, monkeypatch):
    monkeypatch.setenv("SWINGDESK_SETUP_CODE", "setup-abc-123")
    a = P()
    assert a.get("/healthz").json() == {"ok": True}
    assert a.get("/api/auth/status").json()["setup_code_required"] is True
    r = a.post("/api/auth/signup", json={"email": "stranger@example.com", "password": "correct horse battery"})
    assert r.status_code == 403 and "setup code" in r.json()["detail"]
    r = a.post("/api/auth/signup", json={"email": "owner@example.com", "password": "correct horse battery", "invite": "setup-abc-123"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    assert a.get("/api/auth/status").json()["setup_code_required"] is False


def test_any_master_secret_works_and_public_url_is_used(P, monkeypatch):
    monkeypatch.setenv("SWINGDESK_MASTER_KEY", "aGVsbG8gdGhpcyBpcyBub3QgYSBmZXJuZXQga2V5Kys=")    # random host-generated value
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://tickmark.onrender.com")
    a = P()
    u = signup(a, "owner@example.com")
    from app import credentials
    with portal.as_user(u["id"]):
        credentials.set_secret("x", "y")
        assert credentials.get_secret("x") == "y"
    meta = a.get("/.well-known/oauth-protected-resource").json()
    assert meta["resource"] == "https://tickmark.onrender.com/mcp"


def test_guest_desk_is_view_only_on_real_data_and_converts_on_signup(P, monkeypatch):
    monkeypatch.setenv("SWINGDESK_OPEN_SIGNUP", "on")
    admin = P()
    signup(admin, "owner@example.com")
    g = P()
    me = g.post("/api/auth/guest").json()["user"]
    assert me["role"] == "guest" and g.get("/api/auth/status").json()["guest"] is True
    assert g.get("/api/status").json()["data_mode"] == "live"                     # guests start on real prices
    assert g.get("/api/watchlist/screen").status_code == 200
    for path, body in (("/api/broker/connect", {"key_id": "k", "secret": "s"}), ("/api/copilot/chat", {"text": "hi"}),
                       ("/api/linked", {}), ("/api/live-gate/enable", {})):
        r = g.post(path, json=body)
        assert r.status_code == 403 and r.json().get("signup") is True, path
    assert g.put("/api/watchlist", json={"symbols": ["NVDA"]}).status_code != 403   # paper desk settings stay usable
    assert g.post("/api/auth/login", json={"email": me["email"], "password": "!"}).status_code == 401   # guests can't sign in by email
    assert admin.get("/api/auth/status").json()["first_user"] is False
    r = g.post("/api/auth/signup", json={"email": "new@example.com", "password": "correct horse battery", "remember": False})
    assert r.status_code == 200 and r.json()["kept_guest_desk"] is True and r.json()["user"]["id"] == me["id"]
    assert "max-age" not in r.headers["set-cookie"].lower()                   # no "Remember me": ends with the browser
    assert g.get("/api/auth/status").json()["user"]["role"] == "user"
    assert [u["email"] for u in admin.get("/api/portal/users").json()] == ["owner@example.com", "new@example.com"]


def test_remember_me_sets_a_30_day_cookie(P):
    a = P()
    signup(a, "owner@example.com")
    b = P()
    r = b.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct horse battery", "remember": True})
    assert "max-age=2592000" in r.headers["set-cookie"].lower()
    r = b.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct horse battery", "remember": False})
    assert "max-age" not in r.headers["set-cookie"].lower()
