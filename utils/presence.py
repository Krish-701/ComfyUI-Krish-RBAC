"""Track recently active (online) users from authenticated requests."""
from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.RLock()
# username -> last_seen_unix
_presence: dict[str, float] = {}
ONLINE_WINDOW_SEC = 180  # seen within 3 minutes = online


def touch(username: str | None) -> None:
    if not username:
        return
    with _lock:
        _presence[str(username)] = time.time()


def list_online(window_sec: int = ONLINE_WINDOW_SEC) -> list[dict[str, Any]]:
    now = time.time()
    out = []
    with _lock:
        dead = [u for u, t in _presence.items() if now - t > window_sec * 3]
        for u in dead:
            _presence.pop(u, None)
        for u, t in _presence.items():
            if now - t <= window_sec:
                out.append(
                    {
                        "username": u,
                        "last_seen": t,
                        "seconds_ago": int(now - t),
                    }
                )
    out.sort(key=lambda x: x["last_seen"], reverse=True)
    return out


def online_count(window_sec: int = ONLINE_WINDOW_SEC) -> int:
    return len(list_online(window_sec))
