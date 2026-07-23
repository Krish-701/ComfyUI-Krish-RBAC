"""
Single active session per user.

When a user logs in again (any browser/device), a new session id is issued and
previous JWTs for that user fail validation immediately.
"""

from __future__ import annotations

import secrets
import threading
from typing import Optional

# user_id (str) -> active session id
_lock = threading.Lock()
_sessions: dict[str, str] = {}


def issue_session(user_id: str | None) -> str:
    """Create and store a new session id for this user (invalidates older ones)."""
    key = str(user_id or "").strip()
    sid = secrets.token_hex(16)
    if not key:
        return sid
    with _lock:
        _sessions[key] = sid
    return sid


def get_session(user_id: str | None) -> Optional[str]:
    key = str(user_id or "").strip()
    if not key:
        return None
    with _lock:
        return _sessions.get(key)


def validate_session(user_id: str | None, sid: str | None) -> bool:
    """True if sid is the current active session for this user."""
    key = str(user_id or "").strip()
    if not key or not sid:
        return False
    with _lock:
        return _sessions.get(key) == str(sid)


def clear_session(user_id: str | None) -> None:
    key = str(user_id or "").strip()
    if not key:
        return
    with _lock:
        _sessions.pop(key, None)


def clear_session_if_match(user_id: str | None, sid: str | None) -> bool:
    """
    End the active session only if ``sid`` is the current one.
    Prevents a stale/old browser from killing a newer login elsewhere.
    """
    key = str(user_id or "").strip()
    if not key or not sid:
        return False
    with _lock:
        if _sessions.get(key) == str(sid):
            _sessions.pop(key, None)
            return True
    return False
