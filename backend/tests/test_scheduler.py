from datetime import datetime, timedelta

import pytest

from app import db, scheduler
from app.live_data import ET
from test_live_forward import confirm, go_to, live, world  # noqa: F401  (fixtures)


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 11, 18, 10, 0, tzinfo=ET), datetime(2026, 11, 18, 16, 20, tzinfo=ET)),   # Wed morning -> today
    (datetime(2026, 11, 20, 17, 0, tzinfo=ET), datetime(2026, 11, 23, 16, 20, tzinfo=ET)),   # Fri evening -> Mon
    (datetime(2026, 11, 22, 9, 0, tzinfo=ET), datetime(2026, 11, 23, 16, 20, tzinfo=ET)),    # Sunday -> Mon
    (datetime(2026, 11, 25, 16, 30, tzinfo=ET), datetime(2026, 11, 27, 16, 20, tzinfo=ET)),  # skips Thanksgiving
])
def test_next_run(now, expected):
    assert scheduler.next_run(now) == expected


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make(clock, refresh, stop_after=None):
    calls = {"n": 0}
    s = None

    def sleeper(seconds):
        clock.t += timedelta(seconds=seconds)
        return s._stop.is_set()

    def fn(trigger):
        calls["n"] += 1
        out = refresh(trigger)
        if stop_after and calls["n"] >= stop_after:
            s.stop()
        return out

    s = scheduler.Scheduler(refresh_fn=fn, clock=clock, sleeper=sleeper)
    return s, calls


OK = lambda trigger: {"report": {"NVDA": {"ok": True}}, "as_of": "2026-11-23", "events": []}  # noqa: E731


def test_skips_in_demo_mode(client):
    s, calls = make(Clock(datetime(2026, 11, 23, 16, 20, tzinfo=ET)), OK)
    assert "demo" in s.run_once("t")["result"] and calls["n"] == 0


def test_skips_when_disabled(client):
    db.set_data_mode("live")
    db.set_setting("auto_refresh", False)
    s, calls = make(Clock(datetime(2026, 11, 23, 16, 20, tzinfo=ET)), OK)
    assert "off" in s.run_once("t")["result"] and calls["n"] == 0
    db.set_data_mode("demo")


def test_retries_then_reports_error(client):
    db.set_data_mode("live")

    def boom(trigger):
        raise ConnectionError("yahoo down")

    clock = Clock(datetime(2026, 11, 23, 16, 20, tzinfo=ET))
    s, calls = make(clock, boom)
    last = s.run_once("t")
    assert calls["n"] == scheduler.MAX_RETRIES + 1 and "yahoo down" in last["result"]
    assert clock.t == datetime(2026, 11, 23, 17, 5, tzinfo=ET)   # 3 x 15-minute waits
    db.set_data_mode("demo")


def test_recovers_on_retry(client):
    db.set_data_mode("live")
    seq = iter([ConnectionError("blip"), None])

    def flaky(trigger):
        e = next(seq)
        if e:
            raise e
        return OK(trigger)

    s, _ = make(Clock(datetime(2026, 11, 23, 16, 20, tzinfo=ET)), flaky)
    last = s.run_once("t")
    assert last["result"] == "ok" and last["attempt"] == 2
    db.set_data_mode("demo")


def test_loop_waits_until_1620_then_runs(client):
    db.set_data_mode("live")
    db.set_setting("auto_refresh", True)
    clock = Clock(datetime(2026, 11, 23, 9, 0, tzinfo=ET))
    s, calls = make(clock, OK, stop_after=1)
    scheduler_behind = scheduler.behind
    scheduler.behind = lambda now: False          # no catch-up for this test
    try:
        s._loop()
    finally:
        scheduler.behind = scheduler_behind
    assert calls["n"] == 1 and clock.t >= datetime(2026, 11, 23, 16, 20, tzinfo=ET)
    assert s.last["trigger"] == "scheduled 16:20 ET"
    db.set_data_mode("demo")


def test_end_to_end_scheduled_refresh_fills_and_tracks(live, world):  # noqa: F811
    """Real refresh path (fake Yahoo): the 16:20 run fills a paper order and advances the forward test."""
    confirm(live)
    pv = live.post("/api/previews", json={"symbol": "NVDA"}).json()
    live.post("/api/orders", json={"preview_id": pv["id"], "confirm": True})
    live.get("/api/watchlist/screen")
    go_to(world, "2026-11-23", hour=16)          # Monday 16:00: bar not final yet
    world["clock"] = world["clock"].replace(minute=0)

    def sleeper(seconds):
        world["clock"] += timedelta(seconds=seconds)
        world["today"] = world["clock"].date().isoformat()
        return s._stop.is_set()

    from app.trading import run_live_refresh

    def fn(trigger):
        out = run_live_refresh(trigger)
        s.stop()
        return out

    s = scheduler.Scheduler(refresh_fn=fn, clock=lambda: world["clock"], sleeper=sleeper)
    scheduler_behind = scheduler.behind
    scheduler.behind = lambda now: False
    try:
        s._loop()
    finally:
        scheduler.behind = scheduler_behind
    assert s.last["as_of"] == "2026-11-23" and any("entry filled" in e for e in s.last["events"])
    sig = next(x for x in live.get("/api/forward").json()["signals"] if x["symbol"] == "NVDA")
    assert sig["entry_date"] == "2026-11-23"
    st = live.get("/api/status").json()
    assert st["last_refresh"]["trigger"] == "scheduled 16:20 ET"


def test_catch_up_detects_stale_live_data(live, world):  # noqa: F811
    go_to(world, "2026-11-24")
    assert scheduler.behind(world["clock"]) is True
    live.post("/api/live/refresh")
    assert scheduler.behind(world["clock"]) is False


def test_toggle_endpoint(client):
    assert client.put("/api/scheduler", json={"enabled": False}).json()["enabled"] is False
    assert client.put("/api/scheduler", json={"enabled": True}).json()["enabled"] is True
