"""One-shot live refresh for cron or launchd, for when the server isn't running.

    cd backend && .venv/bin/python -m app.refresh_cli

Exit code 0 on success, 1 if not in Live mode, 2 if any ticker failed to fetch.
"""
import sys

from . import db
from .main import ensure_db
from .trading import TradeError, run_live_refresh


def main() -> int:
    if db.data_mode() != "live":
        print("SwingDesk is in Demo data mode; switch to Live data in the app first.")
        return 1
    ensure_db("live")
    try:
        res = run_live_refresh("cli")
    except TradeError as exc:
        print(f"refresh failed: {exc}")
        return 2
    failed = {k: v["error"] for k, v in res["report"].items() if not v["ok"]}
    print(f"live data through {res['as_of']} close; {len(res['events'])} paper event(s)")
    for e in res["events"]:
        print("  ", e)
    for k, v in failed.items():
        print(f"  {k}: {v}")
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
