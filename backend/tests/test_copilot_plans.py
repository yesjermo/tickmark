"""Copilot plans: free Claude allowance on the operator's key, free open-source model, own key unlimited."""
import json
import pytest
from fastapi.testclient import TestClient

from app import copilot
from test_copilot import FakeClaude, msg
from test_copilot import FakeOllama, oa  # noqa: F401
import httpx


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.setenv("SWINGDESK_FREE_TIER", "on")
    monkeypatch.setenv("SWINGDESK_FREE_MESSAGES", "2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "operator-key")
    f = FakeClaude([msg([f"answer {i}"]) for i in range(10)])
    monkeypatch.setattr(copilot, "CLIENT_FACTORY", None)
    monkeypatch.setattr(copilot, "operator_client", lambda: f)
    return f


def test_free_plan_is_metered_and_resets_by_month(confirmed, operator, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "operator-key")
    st = confirmed.get("/api/copilot").json()
    free = next(p for p in st["plans"] if p["id"] == "free")
    assert free["enabled"] and free["limit"] == 2 and free["used"] == 0
    for i in range(2):
        r = confirmed.post("/api/copilot/chat", json={"text": f"q{i}", "model": "free"})
        assert r.status_code == 200 and r.json()["model"] == "free:claude-opus-5-5"
    assert operator.requests[0]["model"] == "claude-opus-5-5"
    r = confirmed.post("/api/copilot/chat", json={"text": "one more", "model": "free"})
    assert r.status_code == 429 and "resets next month" in r.json()["detail"]
    assert next(p for p in confirmed.get("/api/copilot").json()["plans"] if p["id"] == "free")["used"] == 2


def test_open_source_plan_is_free_and_separate(confirmed, monkeypatch):
    monkeypatch.setenv("SWINGDESK_OSS_BASE_URL", "http://oss.internal:11434/v1")
    monkeypatch.setenv("SWINGDESK_OSS_MODEL", "qwen3.5:9b")
    f = FakeOllama([oa("Open-source answer.")])
    monkeypatch.setattr(copilot, "LOCAL_TRANSPORT", httpx.MockTransport(f.handle))
    monkeypatch.setattr(copilot, "CLIENT_FACTORY", None)
    st = confirmed.get("/api/copilot").json()
    assert st["model"] == "oss" and st["ready"]                                      # no key: defaults to what's free
    r = confirmed.post("/api/copilot/chat", json={"text": "hi"}).json()
    assert r["reply"] == "Open-source answer." and r["model"] == "oss:qwen3.5:9b" and f.requests[0]["model"] == "qwen3.5:9b"


def test_plans_off_by_default_and_own_key_unmetered(confirmed, monkeypatch):
    monkeypatch.setattr(copilot, "CLIENT_FACTORY", None)
    st = confirmed.get("/api/copilot").json()
    assert not any(p["enabled"] for p in st["plans"])
    r = confirmed.post("/api/copilot/chat", json={"text": "x", "model": "free"})
    assert r.status_code == 409 and "isn't offered" in r.json()["detail"]
    f = FakeClaude([msg(["a"]), msg(["b"]), msg(["c"])])
    monkeypatch.setattr(copilot, "CLIENT_FACTORY", lambda: f)
    for _ in range(3):
        assert confirmed.post("/api/copilot/chat", json={"text": "x", "model": "claude-opus-5-5"}).status_code == 200


def test_free_allowance_is_per_portal_user(client, operator, monkeypatch):
    monkeypatch.setenv("SWINGDESK_PORTAL", "on")
    from app import portal
    monkeypatch.setattr(portal, "_fail", {})
    a, b = TestClient(client.app, headers={"x-swingdesk": "1"}), TestClient(client.app, headers={"x-swingdesk": "1"})
    a.post("/api/auth/signup", json={"email": "a@example.com", "password": "correct horse battery"})
    code = a.post("/api/portal/invites", json={}).json()["code"]
    b.post("/api/auth/signup", json={"email": "b@example.com", "password": "correct horse battery", "invite": code})
    assert a.get("/api/copilot").json()["model"] == "free"                         # operator key -> free plan for portal users
    for _ in range(2):
        assert a.post("/api/copilot/chat", json={"text": "x", "model": "free"}).status_code == 200
    assert a.post("/api/copilot/chat", json={"text": "x", "model": "free"}).status_code == 429
    assert b.post("/api/copilot/chat", json={"text": "x", "model": "free"}).status_code == 200   # B has their own allowance


def test_open_source_plan_can_use_a_hosted_service_with_a_key(confirmed, monkeypatch):
    monkeypatch.setenv("SWINGDESK_OSS_BASE_URL", "https://api.groq.example/openai/v1")
    monkeypatch.setenv("SWINGDESK_OSS_MODEL", "qwen/qwen3-32b")
    monkeypatch.setenv("SWINGDESK_OSS_API_KEY", "gsk_test_123")
    seen = {}

    def handle(req):
        seen["auth"] = req.headers.get("authorization")
        seen["model"] = json.loads(req.content)["model"]
        return httpx.Response(200, json=oa("Hosted open model answer."))
    monkeypatch.setattr(copilot, "LOCAL_TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setattr(copilot, "CLIENT_FACTORY", None)
    r = confirmed.post("/api/copilot/chat", json={"text": "hi", "model": "oss"}).json()
    assert r["reply"] == "Hosted open model answer." and seen == {"auth": "Bearer gsk_test_123", "model": "qwen/qwen3-32b"}


def test_free_open_plan_offers_several_models_and_users_pick(confirmed, monkeypatch):
    monkeypatch.setenv("SWINGDESK_OSS_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("SWINGDESK_OSS_MODELS", "gemma4:12b,gpt-oss:20b,qwen3.5:9b")
    seen = []

    def handle(req):
        seen.append(json.loads(req.content)["model"])
        return httpx.Response(200, json=oa("ok"))
    monkeypatch.setattr(copilot, "LOCAL_TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setattr(copilot, "CLIENT_FACTORY", None)
    oss = next(p for p in confirmed.get("/api/copilot").json()["plans"] if p["id"] == "oss")
    assert [m["label"] for m in oss["models"]] == ["Google Gemma 4 12B", "OpenAI gpt-oss 20B", "Qwen 3.5 9B"]
    confirmed.post("/api/copilot/chat", json={"text": "x", "model": "oss"})
    st = confirmed.put("/api/copilot/settings", json={"model": "oss", "oss_model": "gpt-oss:20b"}).json()
    assert st["model_label"] == "Open-source · free (OpenAI gpt-oss 20B)"
    r = confirmed.post("/api/copilot/chat", json={"text": "y"}).json()
    assert seen == ["gemma4:12b", "gpt-oss:20b"] and r["model"] == "oss:gpt-oss:20b"
    assert confirmed.put("/api/copilot/settings", json={"model": "oss", "oss_model": "llama-secret"}).status_code == 400


def test_snapshot_mode_for_models_that_cannot_call_tools(confirmed, monkeypatch):
    """A pruned model (gpt-oss nano) gets the desk's real data in the prompt and no tools, so it can't guess or draft."""
    monkeypatch.setenv("SWINGDESK_OSS_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("SWINGDESK_OSS_MODELS", "gemma4:e2b,gpt-oss-nano:6b")
    sent = []

    def handle(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json=oa("NVDA shows no setup today."))
    monkeypatch.setattr(copilot, "LOCAL_TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setattr(copilot, "CLIENT_FACTORY", None)
    oss = next(p for p in confirmed.get("/api/copilot").json()["plans"] if p["id"] == "oss")
    assert oss["models"][1]["label"] == "OpenAI gpt-oss nano (pruned, snapshot mode) 6B"
    confirmed.put("/api/copilot/settings", json={"model": "oss", "oss_model": "gpt-oss-nano:6b"})
    r = confirmed.post("/api/copilot/chat", json={"text": "Does NVDA qualify?"}).json()
    body = sent[-1]
    assert "tools" not in body and "DATA (fetched just now" in body["messages"][0]["content"]
    assert "get_symbol_evaluation" in body["messages"][0]["content"] and '"NVDA"' in body["messages"][0]["content"]
    assert [s["tool"] for s in r["steps"]] == ["get_overview", "get_positions_and_orders", "get_symbol_evaluation"]
    assert r["reply"].startswith("NVDA shows no setup today.") and "Snapshot mode" in r["reply"] and r["proposals"] == []


def test_server_that_rejects_tools_falls_back_to_snapshot(confirmed, monkeypatch):
    monkeypatch.setenv("SWINGDESK_OSS_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("SWINGDESK_OSS_MODEL", "some-tiny:1b")

    def handle(req):
        if "tools" in json.loads(req.content):
            return httpx.Response(400, json={"error": {"message": "registry.ollama.ai/some-tiny:1b does not support tools"}})
        return httpx.Response(200, json=oa("Answered from data."))
    monkeypatch.setattr(copilot, "LOCAL_TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setattr(copilot, "CLIENT_FACTORY", None)
    r = confirmed.post("/api/copilot/chat", json={"text": "status?", "model": "oss"}).json()
    assert r["reply"].startswith("Answered from data.") and "Snapshot mode" in r["reply"]


def test_free_quota_is_reserved_before_the_call_and_refunded_on_failure(confirmed, monkeypatch):
    from app import copilot_plans
    monkeypatch.setenv("SWINGDESK_OSS_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("SWINGDESK_OSS_DAILY", "1")

    def boom(req):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(copilot, "LOCAL_TRANSPORT", httpx.MockTransport(boom))
    monkeypatch.setattr(copilot, "CLIENT_FACTORY", None)
    assert confirmed.post("/api/copilot/chat", json={"text": "x", "model": "oss"}).status_code == 502
    assert copilot_plans.used("oss") == 0                                      # refunded
    monkeypatch.setattr(copilot, "LOCAL_TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(200, json=oa("ok"))))
    assert confirmed.post("/api/copilot/chat", json={"text": "x", "model": "oss"}).status_code == 200
    assert copilot_plans.used("oss") == 1
    assert confirmed.post("/api/copilot/chat", json={"text": "y", "model": "oss"}).status_code == 429   # limit 1 reached
