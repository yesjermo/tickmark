"""OAuth for MCP clients: discovery, dynamic registration, sign-in + consent, PKCE, refresh rotation, revocation."""
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import portal


def pkce():
    v = secrets.token_urlsafe(48)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def rpc(c, tok, method="tools/list"):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method}, headers={"Authorization": f"Bearer {tok}"})


def connect(ai, browser, redirect="https://claude.ai/api/mcp/auth_callback"):
    """What an AI app does: discover, register, send the user to authorize, exchange the code."""
    assert ai.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).headers["www-authenticate"].count("resource_metadata")
    meta = ai.get("/.well-known/oauth-protected-resource").json()
    asm = ai.get("/.well-known/oauth-authorization-server").json()
    assert meta["resource"].endswith("/mcp") and asm["code_challenge_methods_supported"] == ["S256"]
    cl = ai.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [redirect]})
    assert cl.status_code == 201
    cid = cl.json()["client_id"]
    v, ch = pkce()
    q = {"response_type": "code", "client_id": cid, "redirect_uri": redirect, "code_challenge": ch,
         "code_challenge_method": "S256", "state": "xyz", "resource": meta["resource"]}
    page = browser.get("/oauth/authorize", params=q, follow_redirects=False)
    return cid, v, q, page


def approve(browser, page):
    rid = page.text.split('name="request_id" value="')[1].split('"')[0]
    r = browser.post("/oauth/authorize", content=f"request_id={rid}&decision=approve",
                     headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert r.status_code == 302
    back = parse_qs(urlparse(r.headers["location"]).query)
    assert back["state"] == ["xyz"]
    return back["code"][0]


def exchange(ai, cid, v, q, code):
    return ai.post("/oauth/token", content=f"grant_type=authorization_code&code={code}&client_id={cid}&redirect_uri={q['redirect_uri']}&code_verifier={v}",
                   headers={"content-type": "application/x-www-form-urlencoded"})


def test_full_flow_single_user(client):
    cid, v, q, page = connect(client, client)
    assert page.status_code == 200 and "Connect Claude to SwingDesk?" in page.text and "can't</b> place" in page.text
    code = approve(client, page)
    bad = client.post("/oauth/token", content=f"grant_type=authorization_code&code={code}&client_id={cid}&redirect_uri={q['redirect_uri']}&code_verifier=wrong",
                      headers={"content-type": "application/x-www-form-urlencoded"})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    assert exchange(client, cid, v, q, code).status_code == 400                       # a failed attempt burns the code
    cid2, v2, q2, page2 = connect(client, client)
    t = exchange(client, cid2, v2, q2, approve(client, page2))
    assert t.status_code == 200 and t.json()["access_token"].startswith("sdo_")
    assert exchange(client, cid, v2, q2, approve(client, connect(client, client)[3])).status_code == 400   # other client's code


def test_flow_with_pkce_refresh_and_revoke(client):
    cid, v, q, page = connect(client, client)
    code = approve(client, page)
    tok = exchange(client, cid, v, q, code).json()
    assert tok["token_type"] == "Bearer" and tok["access_token"].startswith("sdo_") and tok["expires_in"] == 3600
    assert rpc(client, tok["access_token"]).status_code == 200
    assert exchange(client, cid, v, q, code).status_code == 400                       # codes are single-use
    r = client.post("/oauth/token", content=f"grant_type=refresh_token&refresh_token={tok['refresh_token']}&client_id={cid}",
                    headers={"content-type": "application/x-www-form-urlencoded"}).json()
    assert r["access_token"] != tok["access_token"]
    assert rpc(client, tok["access_token"]).status_code == 401                       # rotated: old pair revoked
    again = client.post("/oauth/token", content=f"grant_type=refresh_token&refresh_token={tok['refresh_token']}&client_id={cid}",
                        headers={"content-type": "application/x-www-form-urlencoded"})
    assert again.status_code == 400
    apps = client.get("/api/mcp/apps").json()
    assert apps[0]["name"] == "Claude"
    client.delete(f"/api/mcp/apps/{cid}")
    assert rpc(client, r["access_token"]).status_code == 401 and client.get("/api/mcp/apps").json() == []


def test_registration_rules_and_deny(client):
    assert client.post("/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]}).status_code == 400
    assert client.post("/oauth/register", json={"redirect_uris": ["https://ok.example/cb"], "token_endpoint_auth_method": "client_secret_basic"}).status_code == 400
    cid, v, q, page = connect(client, client, redirect="http://localhost:3333/cb")
    rid = page.text.split('name="request_id" value="')[1].split('"')[0]
    r = client.post("/oauth/authorize", content=f"request_id={rid}&decision=deny", headers={"content-type": "application/x-www-form-urlencoded"},
                    follow_redirects=False)
    assert "error=access_denied" in r.headers["location"]
    q2 = {**q, "redirect_uri": "https://attacker.example/cb"}
    assert client.get("/oauth/authorize", params=q2).status_code == 400              # unregistered redirect: shown, never redirected


def test_portal_requires_signin_and_scopes_to_that_user(client, monkeypatch):
    monkeypatch.setenv("SWINGDESK_PORTAL", "on")
    monkeypatch.setattr(portal, "_fail", {})
    browser = TestClient(client.app, headers={"x-swingdesk": "1"})
    ai = TestClient(client.app)
    cid, v, q, page = connect(ai, browser)
    assert page.status_code == 302 and page.headers["location"].startswith("/?next=%2Foauth%2Fauthorize")
    browser.post("/api/auth/signup", json={"email": "trader@example.com", "password": "correct horse battery"})
    page = browser.get("/oauth/authorize", params=q)
    assert "Signed in as trader@example.com" in page.text
    tok = exchange(ai, cid, v, q, approve(browser, page)).json()
    ov = ai.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_risk", "arguments": {}}},
                 headers={"Authorization": f"Bearer {tok['access_token']}"})
    assert ov.status_code == 200 and ov.json()["result"]["isError"] is False
    assert browser.get("/api/mcp/apps").json()[0]["client_id"] == cid
