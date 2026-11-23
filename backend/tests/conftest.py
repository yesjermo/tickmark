import pytest
from fastapi.testclient import TestClient

from app import credentials, db
from app.brokers import alpaca


class MemoryKeyring:
    """Stands in for the OS keychain so tests never read or write real credentials."""
    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.delenv("SWINGDESK_LLM", raising=False)
    monkeypatch.setenv("SWINGDESK_SCHEDULER", "off")
    monkeypatch.setenv("SWINGDESK_STREAM", "off")
    monkeypatch.setenv("SWINGDESK_DAYMON", "off")
    monkeypatch.setattr(credentials, "_backend", MemoryKeyring())
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    monkeypatch.setattr(alpaca, "TRANSPORT", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)      # the copilot must never reach the real API in tests
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    from app import copilot
    monkeypatch.setattr(copilot, "CLIENT_FACTORY", None)
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def confirmed(client):
    d = client.get("/api/rulesets").json()["draft"]
    r = client.post("/api/rulesets", json={"text": d["source_text"], "params": d["params"], "interpretations": d["interpretations"]})
    assert r.status_code == 200
    return client


def confirm_with(client, **overrides):
    d = client.get("/api/rulesets").json()["draft"]
    return client.post("/api/rulesets", json={"text": d["source_text"], "params": {**d["params"], **overrides},
                                              "interpretations": d["interpretations"]}).json()
