from datetime import datetime, timedelta, timezone

from app import portfolio


def test_portfolio_balance_allocation_positions_and_history(confirmed, monkeypatch):
    clock = {"t": datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(portfolio, "NOW", lambda: clock["t"])
    p = confirmed.get("/api/portfolio").json()
    assert p["total_usd"] == p["trading"]["equity"] == 100000.0 and p["allocation"] == [{"name": "Cash", "kind": "cash", "usd": 100000.0, "pct": 100.0}]
    assert len(p["history"]) == 1

    pv = confirmed.post("/api/previews", json={"symbol": "NVDA", "overrides": {}}).json()
    confirmed.post("/api/orders", json={"preview_id": pv["id"], "confirm": True, "thesis": "test"})
    confirmed.post("/api/sim/advance")
    clock["t"] += timedelta(minutes=5)
    p = confirmed.get("/api/portfolio").json()
    pos = p["positions"][0]
    assert pos["symbol"] == "NVDA" and pos["avg_cost"] and pos["unrealized"] == round((pos["last"] - pos["avg_cost"]) * pos["qty"], 2)
    assert {a["name"] for a in p["allocation"]} == {"Cash", "NVDA"} and abs(sum(a["pct"] for a in p["allocation"]) - 100) < 0.05
    assert len(p["history"]) == 1                                   # sampled at most every 15 minutes
    clock["t"] += timedelta(minutes=15)
    assert len(confirmed.get("/api/portfolio").json()["history"]) == 2
