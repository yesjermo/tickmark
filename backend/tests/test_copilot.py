"""Copilot: the tool loop, read tools, proposals, and that nothing changes until the user applies."""
import json

import pytest
from anthropic.types.beta import BetaMessage

from app import copilot, db

N = 0


def msg(content, stop="end_turn"):
    global N
    N += 1
    blocks = []
    for b in content:
        if isinstance(b, str):
            blocks.append({"type": "text", "text": b})
        else:
            name, args = b
            blocks.append({"type": "tool_use", "id": f"tu_{N}_{name}", "name": name, "input": args})
    return BetaMessage.model_validate({"id": f"msg_{N}", "type": "message", "role": "assistant", "model": copilot.MODEL,
                                       "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, *blocks],
                                       "stop_reason": stop, "stop_sequence": None,
                                       "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 80}})


class FakeClaude:
    """Returns scripted responses in order and records every request."""
    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.beta = self
        self.messages = self

    def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, default=str)))
        return self.script.pop(0)


@pytest.fixture
def claude(monkeypatch):
    holder = {}

    def use(*script):
        holder["c"] = FakeClaude(script)
        monkeypatch.setattr(copilot, "CLIENT_FACTORY", lambda: holder["c"])
        return holder["c"]
    return use


def chat(c, text, tid=None):
    r = c.post("/api/copilot/chat", json={"text": text, "thread_id": tid})
    assert r.status_code == 200, r.text
    return r.json()


def test_needs_a_key(client):
    st = client.get("/api/copilot").json()
    assert st["configured"] is False and st["model"] == "claude-opus-5-5"
    r = client.post("/api/copilot/chat", json={"text": "hi"})
    assert r.status_code == 409 and "API key" in r.json()["detail"]


def test_tool_loop_request_shape_and_results(confirmed, claude):
    fake = claude(msg([("get_overview", {}), ("get_indicators", {"symbol": "NVDA"})], "tool_use"),
                  msg(["NVDA qualifies; RSI and the stop are in the evidence."]))
    out = chat(confirmed, "What qualifies today?")
    assert out["reply"].startswith("NVDA qualifies") and [s["tool"] for s in out["steps"]] == ["get_overview", "get_indicators"]
    assert all(s["ok"] for s in out["steps"])
    first, second = fake.requests
    assert first["model"] == "claude-opus-5-5" and first["thinking"] == {"type": "adaptive"}
    assert first["cache_control"] == {"type": "ephemeral"} and first["fallbacks"] == "default"
    assert "tool_choice" not in first
    # the second request carries the assistant turn unchanged (thinking block included) and both results in ONE user message
    asst, results = second["messages"][-2], second["messages"][-1]
    assert asst["content"][0]["type"] == "thinking" and asst["content"][0]["signature"] == "sig"
    assert [r["type"] for r in results["content"]] == ["tool_result", "tool_result"]
    ov = json.loads(results["content"][0]["content"])
    assert ov["rules"]["version"] == 1 and any(s["symbol"] == "NVDA" and s["status"].startswith("QUALIFIES") for s in ov["screen"])
    ind = json.loads(results["content"][1]["content"])
    assert ind["symbol"] == "NVDA" and ind["rsi14"] is not None and ind["bollinger"]["mid"] is not None and ind["macd"] is not None
    assert out["usage"]["cache_read"] == 160


def test_conversation_persists_and_continues(confirmed, claude):
    claude(msg(["First answer."]))
    tid = chat(confirmed, "hello")["thread_id"]
    fake = claude(msg(["Second answer."]))
    chat(confirmed, "and then?", tid)
    sent = fake.requests[0]["messages"]
    assert len(sent) == 3 and sent[1]["role"] == "assistant" and sent[2]["content"].endswith("and then?")
    th = confirmed.get(f"/api/copilot/threads/{tid}").json()
    assert [t["text"] for t in th["turns"]] == ["hello", "First answer.", "and then?", "Second answer."]


def test_swing_order_proposal_changes_nothing_until_opened(confirmed, claude):
    claude(msg([("propose_swing_order", {"symbol": "NVDA", "qty": None, "stop_price": None, "rationale": "Pullback qualifies."})], "tool_use"),
           msg(["Drafted a ticket for you to review."]))
    out = chat(confirmed, "Set up the NVDA trade")
    (p,) = out["proposals"]
    assert p["kind"] == "swing_order" and p["status"] == "proposed" and p["payload"]["qty"] > 0
    assert "risk" in p["payload"] and p["payload"]["mode"] == "simulator"
    assert confirmed.get("/api/orders").json() == []                 # no order exists
    r = confirmed.post(f"/api/copilot/proposals/{p['id']}/apply").json()
    assert r["open"] == "ticket" and r["symbol"] == "NVDA" and r["proposal"]["status"] == "opened"
    assert confirmed.get("/api/orders").json() == []                 # still none: the ticket is where the user confirms


def test_rule_change_proposal_and_apply(confirmed, claude):
    claude(msg([("propose_rule_change", {"strategy": "ema_pullback", "changes": [{"name": "stop_buffer_pct", "value": 1.5}],
                                         "rationale": "Wider stop."})], "tool_use"), msg(["Proposed."]))
    (p,) = chat(confirmed, "widen my stop buffer")["proposals"]
    assert p["payload"]["changes"] == {"stop_buffer_pct": {"from": 1.0, "to": 1.5}}
    assert confirmed.get("/api/rulesets").json()["active"]["version"] == 1        # unchanged until applied
    r = confirmed.post(f"/api/copilot/proposals/{p['id']}/apply")
    assert r.status_code == 200 and "rules v2" in r.json()["proposal"]["result"]
    act = confirmed.get("/api/rulesets").json()["active"]
    assert act["version"] == 2 and act["params"]["stop_buffer_pct"] == 1.5
    assert confirmed.post(f"/api/copilot/proposals/{p['id']}/apply").status_code == 409          # one-shot


def test_stale_rule_proposal_refused(confirmed, claude):
    claude(msg([("propose_rule_change", {"strategy": "ema_pullback", "changes": [{"name": "hold_sessions", "value": 7}],
                                         "rationale": "x"})], "tool_use"), msg(["ok"]))
    (p,) = chat(confirmed, "hold longer")["proposals"]
    from conftest import confirm_with
    confirm_with(confirmed, hold_sessions=6)                         # user changed rules meanwhile
    r = confirmed.post(f"/api/copilot/proposals/{p['id']}/apply")
    assert r.status_code == 409 and "changed since" in r.json()["detail"]


def test_bad_proposals_come_back_as_tool_errors(confirmed, claude):
    fake = claude(msg([("propose_rule_change", {"strategy": "ema_pullback", "changes": [{"name": "leverage", "value": 10}], "rationale": "x"}),
                       ("propose_risk_limits", {"changes": [{"name": "max_portfolio_heat_pct", "value": 500}], "rationale": "x"}),
                       ("propose_swing_order", {"symbol": "COIN", "qty": None, "stop_price": None, "rationale": "x"})], "tool_use"),
                  msg(["None of those were possible."]))
    out = chat(confirmed, "go wild")
    assert out["proposals"] == [] and not any(s["ok"] for s in out["steps"])
    res = fake.requests[1]["messages"][-1]["content"]
    assert all(r.get("is_error") for r in res)
    assert "isn't a rule parameter" in res[0]["content"] and "between" in res[1]["content"] and "does not qualify" in res[2]["content"]


def test_risk_limit_proposal_apply(confirmed, claude):
    claude(msg([("propose_risk_limits", {"changes": [{"name": "max_open_positions", "value": 3}], "rationale": "Fewer positions."})], "tool_use"),
           msg(["ok"]))
    (p,) = chat(confirmed, "limit me to 3 positions")["proposals"]
    assert confirmed.post(f"/api/copilot/proposals/{p['id']}/apply").status_code == 200
    assert confirmed.get("/api/risk").json()["limits"]["params"]["max_open_positions"] == 3


def test_dismiss(confirmed, claude):
    claude(msg([("propose_risk_limits", {"changes": [{"name": "max_open_positions", "value": 4}], "rationale": "x"})], "tool_use"), msg(["ok"]))
    (p,) = chat(confirmed, "x")["proposals"]
    assert confirmed.post(f"/api/copilot/proposals/{p['id']}/dismiss").json()["status"] == "dismissed"
    assert confirmed.post(f"/api/copilot/proposals/{p['id']}/apply").status_code == 409


def test_no_tool_can_place_or_override():
    names = {t["name"] for t in copilot.TOOLS}
    assert not any(w in n for n in names for w in ("submit", "kill", "override", "mode", "gate", "key"))
    assert all(n.startswith(("get_", "list_", "propose_")) for n in names)


def test_refusal_and_step_limit(confirmed, claude, monkeypatch):
    claude(msg([], "refusal"))
    assert "declined" in chat(confirmed, "x")["reply"]
    monkeypatch.setattr(copilot, "MAX_STEPS", 2)
    claude(msg([("get_risk", {})], "tool_use"), msg([("get_risk", {})], "tool_use"))
    assert "too many lookups" in chat(confirmed, "loop")["reply"]


def test_key_saved_to_keychain_only(client, monkeypatch):
    import anthropic

    class Models:
        def retrieve(self, m):
            return {"id": m}

    class Fake:
        def __init__(self, **kw):
            self.models = Models()
    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    r = client.put("/api/copilot/key", json={"key": "sk-ant-test-123456"})
    assert r.status_code == 200 and r.json()["source"] == "keychain" and r.json()["key_hint"] == "sk…3456"
    assert "sk-ant-test-123456" not in json.dumps(client.get("/api/copilot").json())
    assert client.delete("/api/copilot/key").json()["configured"] is False


def test_exit_proposal_cancel_applies_through_normal_path(confirmed, claude):
    pv = confirmed.post("/api/previews", json={"symbol": "NVDA"}).json()
    oid = confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True}).json()["order"]["id"]
    claude(msg([("propose_exit", {"order_id": oid, "action": "flatten", "rationale": "x"}),
                ("propose_exit", {"order_id": oid, "action": "cancel", "rationale": "Setup invalidated."})], "tool_use"), msg(["ok"]))
    out = chat(confirmed, "cancel my NVDA order")
    assert [s["ok"] for s in out["steps"]] == [False, True]          # flatten is for day trades only
    (p,) = out["proposals"]
    assert next(o for o in confirmed.get("/api/orders").json() if o["id"] == oid)["status"] == "pending"
    assert confirmed.post(f"/api/copilot/proposals/{p['id']}/apply").json()["proposal"]["status"] == "applied"
    assert next(o for o in confirmed.get("/api/orders").json() if o["id"] == oid)["status"] == "canceled"


# ---------- model choice ----------

def test_pick_claude_model_per_request_and_haiku_has_no_fallback(confirmed, claude):
    fake = claude(msg(["ok"]), msg(["ok"]))
    r = confirmed.post("/api/copilot/chat", json={"text": "x", "model": "claude-sonnet-5-5"}).json()
    assert r["model"] == "claude-sonnet-5-5" and fake.requests[0]["model"] == "claude-sonnet-5-5"
    assert fake.requests[0]["fallbacks"] == "default"
    confirmed.put("/api/copilot/settings", json={"model": "claude-haiku-5-5"})
    chat(confirmed, "y")
    assert fake.requests[1]["model"] == "claude-haiku-5-5" and "fallbacks" not in fake.requests[1]
    assert confirmed.put("/api/copilot/settings", json={"model": "gpt-9"}).status_code == 400


import httpx  # noqa: E402


class FakeOllama:
    """OpenAI-compatible /v1/chat/completions + /v1/models, scripted."""
    def __init__(self, script, models=("qwen3.5:9b",)):
        self.script, self.models, self.requests = list(script), models, []

    def handle(self, req: httpx.Request):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": m} for m in self.models]})
        body = json.loads(req.content)
        self.requests.append(body)
        if body["model"] not in self.models:
            return httpx.Response(404, json={"error": "model not found"})
        return httpx.Response(200, json=self.script.pop(0))


def oa(content=None, calls=()):
    tc = [{"id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": a if isinstance(a, str) else json.dumps(a)}}
          for i, (n, a) in enumerate(calls)]
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content, **({"tool_calls": tc} if tc else {})},
                         "finish_reason": "tool_calls" if tc else "stop"}], "usage": {"prompt_tokens": 50, "completion_tokens": 10}}


@pytest.fixture
def ollama(monkeypatch):
    def use(*script, models=("qwen3.5:9b",)):
        f = FakeOllama(script, models)
        monkeypatch.setattr(copilot, "LOCAL_TRANSPORT", httpx.MockTransport(f.handle))
        return f
    return use


def test_local_model_tool_loop_without_anthropic_key(confirmed, ollama):
    f = ollama(oa(None, [("get_overview", {}), ("get_indicators", '{"symbol": "NVDA"}')]),
               oa(None, [("propose_rule_change", "{not json")]),
               oa("<think>let me see</think>NVDA qualifies on the latest bar."))
    st = confirmed.put("/api/copilot/settings", json={"model": "local", "local_model": "qwen3.5:9b"}).json()
    assert st["configured"] is True and st["model_label"] == "Local: qwen3.5:9b" and st["local_loopback"] is True
    out = chat(confirmed, "what qualifies?")
    assert out["reply"] == "NVDA qualifies on the latest bar." and out["model"] == "local:qwen3.5:9b"
    assert [s["ok"] for s in out["steps"]] == [True, True, False]           # bad JSON args come back as a tool error
    first, second, third = f.requests
    assert first["model"] == "qwen3.5:9b" and first["messages"][0]["role"] == "system"
    assert {t["function"]["name"] for t in first["tools"]} == {t["name"] for t in copilot.TOOLS}
    tool_msgs = [m for m in second["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_0", "call_1"]
    assert json.loads(tool_msgs[1]["content"])["symbol"] == "NVDA"
    assert "not valid JSON" in [m for m in third["messages"] if m["role"] == "tool"][-1]["content"]
    th = confirmed.get(f"/api/copilot/threads/{out['thread_id']}").json()
    assert th["model"] == "local:qwen3.5:9b" and th["turns"][-1]["text"] == "NVDA qualifies on the latest bar."


def test_local_proposals_have_the_same_guardrails(confirmed, ollama):
    ollama(oa(None, [("propose_swing_order", {"symbol": "NVDA", "qty": None, "stop_price": None, "rationale": "x"})]), oa("Drafted."))
    confirmed.put("/api/copilot/settings", json={"model": "local"})
    (p,) = chat(confirmed, "set it up")["proposals"]
    assert p["status"] == "proposed" and confirmed.get("/api/orders").json() == []


def test_switching_families_needs_a_new_conversation(confirmed, claude, ollama):
    claude(msg(["hi from Claude"]))
    tid = chat(confirmed, "hi")["thread_id"]
    ollama(oa("hi"))
    r = confirmed.post("/api/copilot/chat", json={"text": "again", "thread_id": tid, "model": "local"})
    assert r.status_code == 409 and "new conversation" in r.json()["detail"]


def test_local_server_errors_are_readable(confirmed, ollama, monkeypatch):
    ollama(models=("qwen3.5:4b",))
    confirmed.put("/api/copilot/settings", json={"model": "local", "local_model": "qwen3.5:9b"})
    r = confirmed.post("/api/copilot/chat", json={"text": "x"})
    assert r.status_code == 502 and "ollama pull qwen3.5:9b" in r.json()["detail"]
    lm = confirmed.get("/api/copilot/local-models").json()
    assert lm["reachable"] and lm["models"] == ["qwen3.5:4b"]

    def refuse(req):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(copilot, "LOCAL_TRANSPORT", httpx.MockTransport(refuse))
    r = confirmed.post("/api/copilot/chat", json={"text": "x"})
    assert r.status_code == 502 and "Start Ollama" in r.json()["detail"]
    assert confirmed.get("/api/copilot/local-models").json()["reachable"] is False
    assert confirmed.put("/api/copilot/settings", json={"model": "local", "local_base_url": "ftp://x"}).status_code == 400
