"""Structured admin audit log (who did what, when).

Stored at users/audit_log.json — separate from the free-text usgromana.log.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

_lock = threading.RLock()
_MAX = 5000
_instance = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class AuditLog:
    def __init__(self, path: str, max_entries: int = _MAX):
        self.path = path
        self.max_entries = max(200, int(max_entries))
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.isfile(path):
            self._write({"entries": [], "version": 1})

    def _read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"entries": [], "version": 1}
            if not isinstance(data.get("entries"), list):
                data["entries"] = []
            return data
        except (OSError, json.JSONDecodeError):
            return {"entries": [], "version": 1}

    def _write(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def add(
        self,
        *,
        action: str,
        actor: str | None = None,
        target: str | None = None,
        detail: str | None = None,
        meta: dict | None = None,
        ip: str | None = None,
    ) -> dict:
        entry = {
            "id": str(uuid.uuid4()),
            "ts": _utc_now(),
            "ts_unix": time.time(),
            "action": action,
            "actor": actor or "system",
            "target": target or "",
            "detail": detail or "",
            "ip": ip or "",
            "meta": meta or {},
        }
        with _lock:
            data = self._read()
            data["entries"].append(entry)
            if len(data["entries"]) > self.max_entries:
                data["entries"] = data["entries"][-self.max_entries :]
            self._write(data)
        return entry

    def list_entries(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        action: str | None = None,
        actor: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit or 200), 2000))
        offset = max(0, int(offset or 0))
        with _lock:
            entries = list(self._read().get("entries") or [])
        entries.reverse()  # newest first
        if action:
            entries = [e for e in entries if e.get("action") == action]
        if actor:
            al = actor.lower()
            entries = [e for e in entries if (e.get("actor") or "").lower() == al]
        if search and search.strip():
            n = search.strip().lower()
            entries = [
                e
                for e in entries
                if n in json.dumps(e, ensure_ascii=False).lower()
            ]
        total = len(entries)
        page = entries[offset : offset + limit]
        return {"entries": page, "total": total, "limit": limit, "offset": offset}

    def export_csv(self, **filters) -> str:
        import csv
        import io

        result = self.list_entries(limit=50000, offset=0, **filters)
        buf = io.StringIO()
        buf.write("\ufeff")
        fields = ["ts", "action", "actor", "target", "detail", "ip", "id"]
        w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for e in result["entries"]:
            w.writerow({k: e.get(k, "") for k in fields})
        return buf.getvalue()

    def clear(self) -> int:
        with _lock:
            data = self._read()
            n = len(data.get("entries") or [])
            data["entries"] = []
            self._write(data)
            return n


def get_audit_log() -> AuditLog:
    global _instance
    if _instance is None:
        from ..constants import CURRENT_DIR

        path = os.path.join(CURRENT_DIR, "users", "audit_log.json")
        _instance = AuditLog(path)
    return _instance


def audit(
    action: str,
    *,
    actor: str | None = None,
    target: str | None = None,
    detail: str | None = None,
    meta: dict | None = None,
    ip: str | None = None,
) -> dict:
    try:
        return get_audit_log().add(
            action=action,
            actor=actor,
            target=target,
            detail=detail,
            meta=meta,
            ip=ip,
        )
    except Exception as e:
        print(f"[Usgromana] audit log failed: {e}")
        return {}
