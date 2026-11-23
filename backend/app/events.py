"""Tiny change counter the UI subscribes to (Server-Sent Events), so pushed order updates,
scheduled refreshes and alerts show up without a manual reload."""
import threading

_lock = threading.Lock()
_version = 0
_last_reason = ""


def bump(reason: str = "") -> int:
    global _version, _last_reason
    with _lock:
        _version += 1
        _last_reason = reason
        return _version


def current() -> tuple[int, str]:
    with _lock:
        return _version, _last_reason
