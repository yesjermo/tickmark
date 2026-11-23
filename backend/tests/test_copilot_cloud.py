"""GPT and Gemini as copilot models: key check, live model list, tool loop, Gemini schema, errors."""
import json

import httpx
import pytest

from app import copilot, credentials
from test_copilot import oa

KEYS = {"openai": "sk-openai-test-123456", "gemini": "AIza-gemini-test-123456"}


class FakeCloud:
    """Answers like api.openai.com/v1 and Gemini's /v1beta/openai endpoints."""
    def __init__(self, script=(), status=200):
        self.script, self.requests, self.status = list(script), [], status

    def handle(self, req: httpx.Request):
        host = req.url.host
        prov = "openai" if host == "api.openai.com" else "gemini"
        if req.headers.get("authorization") != f"Bearer {KEYS[prov]}":
            return httpx.Response(401, json={"error": {"message": "invalid key"}})
        if req.url.path.endswith("/models"):
            ids = (["gpt-5", "gpt-5-mini", "text-embedding-3-small", "gpt-4o-realtime-preview", "dall-e-3", "o4-mini"] if prov == "openai"
                   else ["models/gemini-2.5-pro", "models/gemini-2.5-flash", "models/text-embedding-004", "models/gemini-2.5-flash-image"])
            return httpx.Response(200, json={"object": "list", "data": [{"id": i} for i in ids]})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "nope"}})
        body = json.loads(req.content)
        self.requests.append((prov, req.url.path, body))
        return httpx.Response(200, json=self.script.pop(0))


@pytest.fixture
def cloud(monkeypatch):
    def use(*script, status=200):
        f = FakeCloud(script, status)
        monkeypatch.setattr(copilot, "CLOUD_TRANSPORT", httpx.MockTransport(f.handle))
        return f
    return use


def test_key_is_checked_models_listed_and_key_kept_private(confirmed, cloud):
    cloud()
    bad = confirmed.put("/api/copilot/providers/openai/key", json={"key": "sk-wrong"})
    assert bad.status_code == 400 and "rejected" in bad.json()["detail"]
    st = confirmed.put("/api/copilot/providers/openai/key", json={"key": KEYS["openai"]}).json()
    assert st["cloud"]["openai"]["configured"] and st["cloud"]["openai"]["model"] == "gpt-5"        # newest flagship first
    models = confirmed.get("/api/copilot/providers/openai/models").json()["models"]
    assert models == ["gpt-5", "gpt-5-mini", "o4-mini"]                                         # chat models only
    gm = (confirmed.put("/api/copilot/providers/gemini/key", json={"key": KEYS["gemini"]}), confirmed.get("/api/copilot/providers/gemini/models").json()["models"])[1]
    assert gm == ["gemini-2.5-pro", "gemini-2.5-flash"]
    assert KEYS["openai"] not in json.dumps(confirmed.get("/api/copilot").json())
    assert credentials.get_secret("openai_api_key") == KEYS["openai"]
    assert confirmed.delete("/api/copilot/providers/openai/key").json()["cloud"]["openai"]["configured"] is False


def test_gpt_runs_the_tool_loop_with_the_users_key(confirmed, cloud):
    f = cloud(oa(None, [("get_indicators", {"symbol": "NVDA"})]), oa("NVDA's RSI14 is in the indicator data."))
    confirmed.put("/api/copilot/providers/openai/key", json={"key": KEYS["openai"]})
    confirmed.put("/api/copilot/settings", json={"model": "openai", "openai_model": "gpt-5"})
    out = confirmed.post("/api/copilot/chat", json={"text": "NVDA RSI?"}).json()
    assert out["model"] == "openai:gpt-5" and out["reply"].startswith("NVDA's RSI14") and out["steps"][0]["ok"]
    prov, path, body = f.requests[0]
    assert prov == "openai" and path == "/v1/chat/completions" and body["model"] == "gpt-5"
    assert "small local model" not in body["messages"][0]["content"]                               # no local-model hint for GPT
    assert any("additionalProperties" in json.dumps(t) for t in body["tools"])                    # OpenAI keeps the full schema
    assert confirmed.get("/api/copilot").json()["model_label"] == "OpenAI GPT: gpt-5 · your key"


def test_gemini_gets_its_schema_subset_and_can_follow_a_local_conversation(confirmed, cloud, monkeypatch):
    from test_copilot import FakeOllama
    lo = FakeOllama([oa("Local answer.")])
    monkeypatch.setattr(copilot, "LOCAL_TRANSPORT", httpx.MockTransport(lo.handle))
    confirmed.put("/api/copilot/settings", json={"model": "local", "local_model": "qwen3.5:9b"})
    tid = confirmed.post("/api/copilot/chat", json={"text": "hi"}).json()["thread_id"]
    f = cloud(oa(None, [("propose_risk_limits", {"changes": [{"name": "restricted", "value": "AAPL, msft"}], "rationale": "x"})]),
              oa("Drafted."))
    confirmed.put("/api/copilot/providers/gemini/key", json={"key": KEYS["gemini"]})
    r = confirmed.post("/api/copilot/chat", json={"text": "restrict AAPL and MSFT", "thread_id": tid, "model": "gemini"})
    assert r.status_code == 200, r.text                                                           # same message format as local
    prov, path, body = f.requests[0]
    assert prov == "gemini" and path == "/v1beta/openai/chat/completions" and body["model"] == "gemini-2.5-pro"
    sent = json.dumps(body["tools"])
    assert "additionalProperties" not in sent and '"type": [' not in sent and '"nullable": true' in sent
    (p,) = r.json()["proposals"]
    assert p["payload"]["params"]["restricted"] == ["AAPL", "MSFT"]                              # text list parsed, not split into letters


def test_errors_are_readable(confirmed, cloud):
    r = confirmed.post("/api/copilot/chat", json={"text": "x", "model": "openai"})
    assert r.status_code == 409 and "API key" in r.json()["detail"]
    cloud(status=429)
    confirmed.put("/api/copilot/providers/openai/key", json={"key": KEYS["openai"]})
    r = confirmed.post("/api/copilot/chat", json={"text": "x", "model": "openai"})
    assert r.status_code == 429 and "quota" in r.json()["detail"]
    assert confirmed.put("/api/copilot/settings", json={"model": "openai", "openai_model": ""}).status_code == 400


# ---------- free GPT / Gemini plans on the server's keys (sign in, no key of your own) ----------

from fastapi.testclient import TestClient  # noqa: E402

from app import copilot_plans, portal  # noqa: E402


@pytest.fixture
def operator_cloud(monkeypatch, cloud):
    monkeypatch.setenv("SWINGDESK_FREE_TIER", "on")
    monkeypatch.setenv("OPENAI_API_KEY", KEYS["openai"])
    monkeypatch.setenv("GEMINI_API_KEY", KEYS["gemini"])
    monkeypatch.setenv("SWINGDESK_FREE_GPT_MESSAGES", "2")
    monkeypatch.setattr(copilot_plans, "_model_cache", {})
    return cloud


def test_free_gpt_and_gemini_run_on_the_server_key_and_pick_the_newest_model(confirmed, operator_cloud):
    f = operator_cloud(oa("GPT answer."), oa("Gemini answer."))
    plans = {p["id"]: p for p in confirmed.get("/api/copilot").json()["plans"]}
    assert plans["free_gpt"]["enabled"] and plans["free_gemini"]["enabled"] and plans["free_gpt"]["model"] == "newest GPT"
    a = confirmed.post("/api/copilot/chat", json={"text": "x", "model": "free_gpt"}).json()
    b = confirmed.post("/api/copilot/chat", json={"text": "y", "model": "free_gemini"}).json()
    assert a["model"] == "free_gpt:gpt-5" and a["reply"] == "GPT answer." and b["model"] == "free_gemini:gemini-2.5-pro"
    assert [x[0] for x in f.requests] == ["openai", "gemini"]
    plans = {p["id"]: p for p in confirmed.get("/api/copilot").json()["plans"]}
    assert plans["free_gpt"]["used"] == 1 and plans["free_gpt"]["model"] == "gpt-5" and plans["free_gemini"]["used"] == 1
    assert credentials.get_secret("openai_api_key") is None                                      # user stored no key


def test_free_gpt_allowance_ends_and_points_to_what_is_left(confirmed, operator_cloud):
    operator_cloud(oa("1"), oa("2"))
    for _ in range(2):
        assert confirmed.post("/api/copilot/chat", json={"text": "x", "model": "free_gpt"}).status_code == 200
    r = confirmed.post("/api/copilot/chat", json={"text": "x", "model": "free_gpt"})
    assert r.status_code == 429 and "Free · Gemini" in r.json()["detail"] and "resets next month" in r.json()["detail"]


def test_each_signed_in_user_gets_their_own_allowance_and_signed_out_gets_nothing(client, operator_cloud, monkeypatch):
    monkeypatch.setenv("SWINGDESK_PORTAL", "on")
    monkeypatch.setattr(portal, "_fail", {})
    operator_cloud(*[oa("ok")] * 5)
    anon = TestClient(client.app, headers={"x-swingdesk": "1"})
    assert anon.post("/api/copilot/chat", json={"text": "x", "model": "free_gpt"}).status_code == 401
    a, b = TestClient(client.app, headers={"x-swingdesk": "1"}), TestClient(client.app, headers={"x-swingdesk": "1"})
    a.post("/api/auth/signup", json={"email": "a@example.com", "password": "correct horse battery"})
    code = a.post("/api/portal/invites", json={}).json()["code"]
    b.post("/api/auth/signup", json={"email": "b@example.com", "password": "correct horse battery", "invite": code})
    assert a.get("/api/copilot").json()["model"] in ("free_gpt", "free_gemini")                 # no key: starts on a free plan
    for _ in range(2):
        assert a.post("/api/copilot/chat", json={"text": "x", "model": "free_gpt"}).status_code == 200
    assert a.post("/api/copilot/chat", json={"text": "x", "model": "free_gpt"}).status_code == 429
    assert b.post("/api/copilot/chat", json={"text": "x", "model": "free_gpt"}).status_code == 200


def test_free_plans_off_unless_the_operator_turns_them_on(confirmed, cloud, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEYS["openai"])                                        # key alone is not enough
    r = confirmed.post("/api/copilot/chat", json={"text": "x", "model": "free_gpt"})
    assert r.status_code == 409 and "isn't offered" in r.json()["detail"]
