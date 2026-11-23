"""Daily live refresh while the server is running.

Runs at 16:20 ET on each trading day (five minutes after the completed-bar cutoff),
so the forward test records every session and paper orders fill without anyone
clicking Refresh. It waits in one-minute steps against the wall clock, so a laptop
that sleeps through 16:20 refreshes as soon as it wakes. On startup it catches up
if the stored data is behind the last completed session. Failed refreshes retry
every 15 minutes, up to 3 times, keeping the cached data in between.

Only runs in Live data mode and only while auto-refresh is enabled. Set
SWINGDESK_SCHEDULER=off to never start the thread (used by the tests). For
refreshes when the server isn't running, see app/refresh_cli.py.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, time

from . import db, live_data
from .tradingcal import is_session, next_sessions

RUN_AT = time(16, 20)
EXIT_AT = time(15, 40)          # broker planned exits: market-on-close must reach Alpaca before 15:50 ET
RETRY_MINUTES = 15
MAX_RETRIES = 3


def next_run(now: datetime, at: time = RUN_AT) -> datetime:
    d = now.date()
    if not (is_session(d) and now.time() < at):
        d = next_sessions(d, 1)[0]
    return datetime.combine(d, at, tzinfo=live_data.ET)


def next_job(now: datetime) -> tuple[datetime, str]:
    """The next of the two daily jobs: 15:40 broker exits, 16:20 data refresh."""
    exits, refresh = next_run(now, EXIT_AT), next_run(now, RUN_AT)
    return (exits, "exits") if exits < refresh else (refresh, "refresh")


def enabled() -> bool:
    return bool(db.get_setting("auto_refresh", True))


def behind(now: datetime) -> bool:
    """True if live data is older than the last completed session."""
    c = db.connect("live")
    try:
        db.init_schema(c)
        a = db.kv_get(c, "as_of")
    finally:
        c.close()
    return a is None or a < live_data.last_completed_session(now).isoformat()


class Scheduler:
    def __init__(self, refresh_fn=None, clock=None, sleeper=None):
        from .trading import run_live_refresh
        self.refresh_fn = refresh_fn or run_live_refresh
        self.clock = clock or live_data.now_et
        self._stop = threading.Event()
        self.sleep = sleeper or self._stop.wait  # returns True if stopped
        self.thread: threading.Thread | None = None
        self.next_run: datetime | None = None
        self.last: dict | None = None
        self.last_exits: dict | None = None
        self.next_job: str | None = None
        self.running = False

    # ---- one attempt (with retries) ----
    def run_once(self, trigger: str) -> dict:
        if not enabled():
            self.last = {"at": self.clock().isoformat(timespec="seconds"), "trigger": trigger, "result": "skipped: auto-refresh off"}
            return self.last
        if db.data_mode() != "live":
            self.last = {"at": self.clock().isoformat(timespec="seconds"), "trigger": trigger, "result": "skipped: demo data mode"}
            return self.last
        self.running = True
        try:
            for attempt in range(1, MAX_RETRIES + 2):
                try:
                    res = self.refresh_fn(trigger)
                    failed = [k for k, v in res["report"].items() if not v["ok"]]
                    self.last = {"at": self.clock().isoformat(timespec="seconds"), "trigger": trigger, "attempt": attempt,
                                 "result": "ok" if not failed else f"partial: {', '.join(failed)} failed",
                                 "as_of": res["as_of"], "events": res["events"]}
                    if not failed:
                        return self.last
                except Exception as exc:
                    self.last = {"at": self.clock().isoformat(timespec="seconds"), "trigger": trigger, "attempt": attempt,
                                 "result": f"error: {type(exc).__name__}: {exc}"[:200]}
                    if attempt == MAX_RETRIES + 1:
                        self._alert("Scheduled refresh failed", self.last["result"])
                if attempt <= MAX_RETRIES and self.sleep(RETRY_MINUTES * 60):
                    break  # stopping
            return self.last
        finally:
            self.running = False

    def run_exits(self, trigger: str) -> dict:
        """Send due planned exits for broker-held positions (Alpaca mode only)."""
        from . import broker_sync
        from .brokers import factory
        at = self.clock().isoformat(timespec="seconds")
        mode = None
        if enabled() and db.data_mode() == "live":
            c = db.connect()
            try:
                db.init_schema(c)
                mode = db.kv_get(c, "broker_mode", "simulator")
            finally:
                c.close()
        if not factory.is_external(mode) or not factory.connected(mode):
            self.last_exits = {"at": at, "trigger": trigger, "result": "skipped: not in Live data + broker mode"}
            return self.last_exits
        try:
            events = broker_sync.run_planned_exits(self.clock())
            self.last_exits = {"at": at, "trigger": trigger, "result": "ok", "events": events}
        except Exception as exc:
            self.last_exits = {"at": at, "trigger": trigger, "result": f"error: {type(exc).__name__}: {exc}"[:200]}
        return self.last_exits

    def _alert(self, title, body):
        from . import alerts
        try:
            with db.tx() as conn:
                alerts.emit(conn, "scheduler", title, body, "critical", None, f"sched:{self.clock().isoformat()[:13]}:{title}")
            alerts.flush()
        except Exception:
            pass

    # ---- loop ----
    def _loop(self):
        from . import portal
        portal.for_each_tenant(lambda: enabled() and db.data_mode() == "live" and behind(self.clock()) and self.run_once("catch-up on startup"))
        while not self._stop.is_set():
            self.next_run, job = next_job(self.clock())
            self.next_job = job
            while not self._stop.is_set():
                remaining = (self.next_run - self.clock()).total_seconds()
                if remaining <= 0:
                    break
                if self.sleep(min(remaining, 60)):
                    return
            if self._stop.is_set():
                return
            from . import alerts, events, oauth, portal

            def job_for_one_user():
                if job == "exits":
                    self.run_exits("scheduled 15:40 ET")
                else:
                    self.run_once("scheduled 16:20 ET")
                try:
                    with db.tx() as conn:
                        oauth.expiry_alerts(conn)
                except Exception:
                    pass
                alerts.flush()
            portal.for_each_tenant(job_for_one_user)       # once per portal user, or once in the single-user app
            events.bump(f"scheduled {job}")

    def start(self):
        if os.environ.get("SWINGDESK_SCHEDULER", "on").lower() in ("off", "0", "false"):
            return
        self.thread = threading.Thread(target=self._loop, name="swingdesk-scheduler", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()

    def state(self) -> dict:
        return {"enabled": enabled(), "thread_alive": bool(self.thread and self.thread.is_alive()),
                "run_at": "16:20 ET data refresh; 15:40 ET broker planned exits (Alpaca mode)",
                "next_run": self.next_run.isoformat() if self.next_run else None, "next_job": self.next_job,
                "last": self.last, "last_exits": self.last_exits, "running": self.running, "data_mode": db.data_mode()}
