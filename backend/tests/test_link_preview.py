"""The served page's link-preview tags name the server that served it, not the author's Render address."""
import pytest

from app import main

pytestmark = pytest.mark.skipif(not main.DIST.exists(), reason="frontend not built (frontend/dist)")


def test_preview_tags_use_public_url(client, monkeypatch):
    monkeypatch.setenv("SWINGDESK_PUBLIC_URL", "https://desk.example.org/")
    for path in ("/", "/index.html", "/some/deep/link"):
        html = client.get(path).text
        assert 'content="https://desk.example.org/og-image.png"' in html
        assert "tickmark-yqj2.onrender.com" not in html


def test_preview_tags_unchanged_on_the_built_origin(client, monkeypatch):
    monkeypatch.setenv("SWINGDESK_PUBLIC_URL", "https://tickmark-yqj2.onrender.com")
    html = client.get("/").text
    assert 'content="https://tickmark-yqj2.onrender.com/og-image.png"' in html
    assert client.get("/").headers["cache-control"] == "no-cache"
