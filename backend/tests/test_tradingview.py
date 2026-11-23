from app import tradingview


def test_tradingview_webhook_records_signals_never_orders(client, monkeypatch):
    monkeypatch.setattr(tradingview, "_hits", {})
    assert client.get("/api/tradingview").json()["connected"] is False
    url = client.post("/api/tradingview/token").json()["url"]
    token = url.rsplit("/", 1)[-1]
    assert url.endswith("/hooks/tradingview/" + token) and token.startswith("tvh_")
    r = client.post(f"/hooks/tradingview/{token}", content=b'{"symbol": "NASDAQ:NVDA", "action": "buy", "price": 125.5, "interval": "1D", "note": "EMA cross"}')
    assert r.status_code == 200 and r.json() == {"ok": True, "symbol": "NVDA", "action": "buy"}
    r = client.post(f"/hooks/tradingview/{token}", content=b"SELL SNDK now")                  # plain-text alerts work too
    assert r.json()["symbol"] == "SNDK" and r.json()["action"] == "sell"
    st = client.get("/api/tradingview").json()
    assert st["connected"] and [s["symbol"] for s in st["signals"]] == ["SNDK", "NVDA"] and st["signals"][1]["price"] == 125.5
    feed = client.get("/api/alerts").json()
    assert any(a["kind"] == "tradingview" and "nothing was ordered" in a["body"] for a in feed["alerts"])
    assert client.get("/api/orders").json() == []                                            # a signal never places an order
    assert client.post(f"/hooks/tradingview/{token}", content=b'{"action": "buy"}').status_code == 422
    assert client.post("/hooks/tradingview/tvh_wrong", content=b'{"symbol": "NVDA"}').status_code == 404
    assert client.post(f"/hooks/tradingview/{token}", content=b"x" * 9000).status_code == 413
    new = client.post("/api/tradingview/token").json()["url"].rsplit("/", 1)[-1]               # rotating kills the old URL
    assert client.post(f"/hooks/tradingview/{token}", content=b'{"symbol": "NVDA"}').status_code == 404
    assert client.post(f"/hooks/tradingview/{new}", content=b'{"symbol": "NVDA"}').status_code == 200
    client.delete("/api/tradingview")
    assert client.post(f"/hooks/tradingview/{new}", content=b'{"symbol": "NVDA"}').status_code == 404


def test_tradingview_rate_limit(client, monkeypatch):
    monkeypatch.setattr(tradingview, "_hits", {})
    monkeypatch.setattr(tradingview, "RATE", (2, 60.0))
    token = client.post("/api/tradingview/token").json()["url"].rsplit("/", 1)[-1]
    codes = [client.post(f"/hooks/tradingview/{token}", content=b'{"symbol": "NVDA"}').status_code for _ in range(3)]
    assert codes == [200, 200, 429]
