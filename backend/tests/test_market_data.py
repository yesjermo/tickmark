"""Licensed market data (Alpaca SIP), split handling, data-quality checks."""
import pandas as pd
import httpx
import pytest

from app import credentials, db, market_data
from fake_alpaca import GOOD_KEY, GOOD_SECRET
from test_live_forward import ROWS, FakeTicker, confirm, go_to, live, world  # noqa: F401  (fixtures)


class FakeAlpacaData:
    """Serves /v2/stocks/bars (paginated, from the reproducible bars) and /v1/corporate-actions."""
    def __init__(self, world):
        self.world = world
        self.refuse_sip = False
        self.splits = []
        self.calls = []

    def handle(self, req):
        self.calls.append((req.url.path, dict(req.url.params)))
        if req.headers.get("APCA-API-KEY-ID") != GOOD_KEY:
            return httpx.Response(401, json={"message": "unauthorized"})
        if req.url.path == "/v2/stocks/bars":
            p = req.url.params
            assert p["feed"] == "sip" and p["adjustment"] == "split" and p["timeframe"] == "1Day"
            if self.refuse_sip:
                return httpx.Response(403, json={"message": "subscription does not permit querying recent SIP data"})
            syms = p["symbols"].split(",")
            rows = [r for r in ROWS if r["symbol"] in syms and r["date"] <= self.world["today"]]
            page = int(p.get("page_token") or 0)
            chunk = rows[page * 400:(page + 1) * 400]          # force several pages
            bars = {}
            for r in chunk:
                bars.setdefault(r["symbol"], []).append({"t": f"{r['date']}T05:00:00Z", "o": r["open"], "h": r["high"],
                                                         "l": r["low"], "c": r["close"], "v": r["volume"]})
            nxt = str(page + 1) if (page + 1) * 400 < len(rows) else None
            return httpx.Response(200, json={"bars": bars, "next_page_token": nxt})
        if req.url.path == "/v1/corporate-actions":
            fwd = [s for s in self.splits if s["new_rate"] > s["old_rate"]]
            return httpx.Response(200, json={"corporate_actions": {"forward_splits": fwd}, "next_page_token": None})
        return httpx.Response(404)


@pytest.fixture
def data(live, world, monkeypatch):
    f = FakeAlpacaData(world)
    monkeypatch.setattr(market_data, "TRANSPORT", httpx.MockTransport(f.handle))
    credentials.save_alpaca(GOOD_KEY, GOOD_SECRET)
    return f


def test_alpaca_provider_needs_keys(live):
    r = live.put("/api/data/provider", json={"provider": "alpaca"})
    assert r.status_code == 409 and "Connect Alpaca" in r.json()["detail"]


def test_switch_to_alpaca_sip(live, data):
    r = live.put("/api/data/provider", json={"provider": "alpaca"}).json()
    assert r["refresh"]["provider"] == "alpaca" and r["refresh"]["provider_note"] is None
    pages = [c for c in data.calls if c[0] == "/v2/stocks/bars"]
    assert len(pages) > 1                                       # pagination followed
    st = live.get("/api/status").json()
    assert st["data_provider"] == "alpaca" and "SIP" in st["data_label"]
    rep = live.get("/api/data").json()["live_fetch"]["report"]
    assert all(v["provider"] == "alpaca" for v in rep.values())
    # Earnings dates still come from Yahoo (Alpaca has none): NVDA's next report is known.
    ev = next(x for x in live.get("/api/watchlist/screen").json()["results"] if x["symbol"] == "NVDA")
    assert ev["facts"]["earnings"]["value"] == "2027-02-24" and ev["status"] == "QUALIFIES"
    assert "Alpaca Market Data" in ev["data_timestamp"]


def test_sip_refused_falls_back_to_yahoo(live, data):
    data.refuse_sip = True
    r = live.put("/api/data/provider", json={"provider": "alpaca"}).json()
    assert "using Yahoo" in r["refresh"]["provider_note"] and "IEX" in r["refresh"]["provider_note"]
    rep = live.get("/api/data").json()["live_fetch"]["report"]
    assert all(v["provider"] == "yahoo" for v in rep.values())


def test_split_rescales_open_position_once(live, data, world):
    confirm(live)
    pv = live.post("/api/previews", json={"symbol": "NVDA"}).json()
    oid = live.post("/api/orders", json={"preview_id": pv["id"], "confirm": True}).json()["order"]["id"]
    go_to(world, "2026-11-23")
    live.post("/api/live/refresh")
    before = next(o for o in live.get("/api/orders").json() if o["id"] == oid)
    assert before["status"] == "filled"
    qty0, px0 = before["fills"][0]["qty"], before["fills"][0]["price"]
    stop0 = next(ch for ch in before["children"] if ch["purpose"] == "stop")["stop_price"]
    data.splits = [{"id": "sp1", "symbol": "NVDA", "old_rate": 1, "new_rate": 2, "ex_date": "2026-11-24"}]
    go_to(world, "2026-11-24")
    r = live.post("/api/live/refresh").json()
    assert any("split applied" in e for e in r["splits"])
    after = next(o for o in live.get("/api/orders").json() if o["id"] == oid)
    assert after["fills"][0]["qty"] == qty0 * 2 and after["fills"][0]["price"] == pytest.approx(px0 / 2, abs=1e-3)
    assert next(ch for ch in after["children"] if ch["purpose"] == "stop")["stop_price"] == pytest.approx(stop0 / 2, abs=1e-3)
    j = live.get("/api/journal").json()[0]
    assert j["evidence"]["suggested_stop"] == pv["stop_price"]          # entry snapshot untouched
    live.post("/api/live/refresh")                                       # idempotent: not applied twice
    again = next(o for o in live.get("/api/orders").json() if o["id"] == oid)
    assert again["fills"][0]["qty"] == qty0 * 2
    ca = live.get("/api/data").json()["corporate_actions"]
    assert ca[0]["symbol"] == "NVDA" and ca[0]["label"] == "2-for-1" and ca[0]["touched"] >= 1
    assert any(a["title"].startswith("Split: NVDA") for a in live.get("/api/alerts").json()["alerts"])


def test_yahoo_split_history_used_without_alpaca(live, world, monkeypatch):
    confirm(live)
    pv = live.post("/api/previews", json={"symbol": "NVDA"}).json()
    live.post("/api/orders", json={"preview_id": pv["id"], "confirm": True})
    go_to(world, "2026-11-23")
    live.post("/api/live/refresh")
    monkeypatch.setattr(FakeTicker, "splits", property(lambda self: pd.Series(
        [3.0] if self.sym == "NVDA" else [], index=pd.DatetimeIndex([pd.Timestamp("2026-11-24")] if self.sym == "NVDA" else []),
        dtype=float)), raising=False)
    go_to(world, "2026-11-24")
    r = live.post("/api/live/refresh").json()
    assert any("NVDA: 3-for-1" in e for e in r["splits"])


def test_impossible_latest_bar_blocks_signal(live, world, monkeypatch):
    real = FakeTicker.history

    def bad(self, *a, **k):
        df = real(self, *a, **k)
        if self.sym == "NVDA" and len(df):
            df.iloc[-1, df.columns.get_loc("Close")] = df.iloc[-1]["High"] * 1.2   # close above the high
        return df
    monkeypatch.setattr(FakeTicker, "history", bad)
    live.post("/api/live/refresh")
    ev = next(x for x in live.get("/api/watchlist/screen").json()["results"] if x["symbol"] == "NVDA")
    assert ev["status"] == "NEEDS_REVIEW" and "impossible bar" in ev["rules"][0]["margin"]
    q = live.get("/api/data").json()["quality"]["symbols"]["NVDA"]
    assert q["ok"] is False and q["latest_issue"]["kind"] == "impossible_bar"
    assert any("Data quality: NVDA" in a["title"] for a in live.get("/api/alerts").json()["alerts"])


def test_quality_flags_missing_session_and_jump(client):
    conn = db.connect()
    db.init_schema(conn)
    days = ["2026-11-16", "2026-11-17", "2026-11-19", "2026-11-20"]       # 11-18 missing
    closes = [100, 101, 102, 160]                                         # +57% on 11-20
    for d, c in zip(days, closes):
        conn.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)", ("XYZ", d, c, c * 1.01, c * 0.99, c, 1e6, "test"))
    rep = market_data.check_quality(conn, ["XYZ"], "2026-11-20")["XYZ"]
    kinds = {(i["kind"], i["date"]) for i in rep["issues"]}
    assert ("missing_session", "2026-11-18") in kinds and ("unexplained_jump", "2026-11-20") in kinds
    conn.execute("INSERT INTO corporate_actions VALUES ('x','XYZ','split',0.5,'2026-11-20','1-for-2','test','now',0)")
    rep2 = market_data.check_quality(conn, ["XYZ"], "2026-11-20")["XYZ"]
    assert not any(i["kind"] == "unexplained_jump" for i in rep2["issues"])   # explained by a recorded split
    conn.close()
