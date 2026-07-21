"""Persistent log of workflow / prompt executions per user.

Stores who ran a job, when, which workflow name, and completion status.
File: users/workflow_runs.json (under the extension root).
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
_MAX_RUNS_DEFAULT = 5000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _default_store() -> dict[str, Any]:
    return {"runs": [], "max_runs": _MAX_RUNS_DEFAULT, "version": 1}


class WorkflowRunLog:
    """Thread-safe append-only-ish run log with size cap."""

    def __init__(self, path: str, max_runs: int = _MAX_RUNS_DEFAULT):
        self.path = path
        self.max_runs = max(100, int(max_runs or _MAX_RUNS_DEFAULT))
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._ensure_file()

    def _ensure_file(self) -> None:
        if not os.path.isfile(self.path):
            self._write(_default_store())

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return _default_store()
            if not isinstance(data.get("runs"), list):
                data["runs"] = []
            return data
        except (OSError, json.JSONDecodeError):
            return _default_store()

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    @staticmethod
    def extract_workflow_name(extra_data: Any, prompt: Any = None) -> str:
        """Best-effort workflow title from ComfyUI extra_data / prompt."""
        if isinstance(extra_data, dict):
            # Injected by our frontend
            for key in ("usgromana_workflow", "workflow_name", "workflowName"):
                val = extra_data.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()[:256]

            nested = extra_data.get("usgromana")
            if isinstance(nested, dict):
                for key in ("workflow_name", "workflowName", "name"):
                    val = nested.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()[:256]

            png = extra_data.get("extra_pnginfo")
            if isinstance(png, dict):
                wf = png.get("workflow")
                if isinstance(wf, dict):
                    # Common Comfy / frontend fields
                    for key in ("filename", "name", "title", "workflow_name"):
                        val = wf.get(key)
                        if isinstance(val, str) and val.strip():
                            return val.strip()[:256]
                    extra = wf.get("extra")
                    if isinstance(extra, dict):
                        for key in ("workflowName", "workflow_name", "name", "title"):
                            val = extra.get(key)
                            if isinstance(val, str) and val.strip():
                                return val.strip()[:256]
                        info = extra.get("info")
                        if isinstance(info, dict):
                            for key in ("name", "title", "workflow_name"):
                                val = info.get(key)
                                if isinstance(val, str) and val.strip():
                                    return val.strip()[:256]

        if isinstance(prompt, dict) and prompt:
            return f"Prompt ({len(prompt)} nodes)"
        return "Unnamed workflow"

    @staticmethod
    def extract_prompt_meta(item: Any) -> dict[str, Any]:
        """
        Parse a ComfyUI queue item tuple:
          (priority, prompt_id, prompt, extra_data, outputs_to_execute[, ...])
        Optionally with trailing Usgromana meta dict.
        """
        meta: dict[str, Any] = {
            "prompt_id": None,
            "workflow_name": "Unnamed workflow",
            "node_count": 0,
            "extra_data": None,
        }
        if not isinstance(item, tuple) or not item:
            return meta

        body = item
        if isinstance(item[-1], dict) and "user_id" in item[-1]:
            body = item[:-1]

        if len(body) >= 2:
            meta["prompt_id"] = body[1]
        prompt = body[2] if len(body) >= 3 else None
        extra = body[3] if len(body) >= 4 else None
        meta["extra_data"] = extra if isinstance(extra, dict) else None
        if isinstance(prompt, dict):
            meta["node_count"] = len(prompt)
        meta["workflow_name"] = WorkflowRunLog.extract_workflow_name(extra, prompt)
        return meta

    def log_queued(
        self,
        *,
        prompt_id: str | None,
        user_id: str | None,
        username: str | None,
        workflow_name: str | None = None,
        node_count: int = 0,
        status: str = "queued",
    ) -> dict[str, Any]:
        """Record a newly queued prompt. Returns the run entry."""
        run = {
            "id": str(uuid.uuid4()),
            "prompt_id": str(prompt_id) if prompt_id else None,
            "user_id": user_id,
            "username": username or "guest",
            "workflow_name": (workflow_name or "Unnamed workflow")[:256],
            "node_count": int(node_count or 0),
            "started_at": _utc_now_iso(),
            "started_ts": time.time(),
            "finished_at": None,
            "finished_ts": None,
            "status": status,
            "duration_sec": None,
        }
        with _lock:
            data = self._read()
            runs: list = data.setdefault("runs", [])
            # Dedup by prompt_id if re-queued oddly
            if run["prompt_id"]:
                for existing in reversed(runs):
                    if existing.get("prompt_id") == run["prompt_id"]:
                        existing.update(
                            {
                                "user_id": run["user_id"],
                                "username": run["username"],
                                "workflow_name": run["workflow_name"],
                                "node_count": run["node_count"],
                                "started_at": run["started_at"],
                                "started_ts": run["started_ts"],
                                "status": status,
                                "finished_at": None,
                                "finished_ts": None,
                                "duration_sec": None,
                            }
                        )
                        self._trim(data)
                        self._write(data)
                        return existing
            runs.append(run)
            self._trim(data)
            self._write(data)
        return run

    def update_status(
        self,
        prompt_id: str | None,
        status: str,
        *,
        finished: bool = False,
    ) -> dict[str, Any] | None:
        if not prompt_id:
            return None
        with _lock:
            data = self._read()
            for run in reversed(data.get("runs", [])):
                if run.get("prompt_id") == str(prompt_id):
                    run["status"] = status
                    if finished:
                        now_ts = time.time()
                        run["finished_at"] = _utc_now_iso()
                        run["finished_ts"] = now_ts
                        start = run.get("started_ts")
                        if isinstance(start, (int, float)):
                            run["duration_sec"] = round(now_ts - start, 3)
                    self._write(data)
                    return run
        return None

    def _trim(self, data: dict[str, Any]) -> None:
        max_runs = int(data.get("max_runs") or self.max_runs)
        runs = data.get("runs") or []
        if len(runs) > max_runs:
            data["runs"] = runs[-max_runs:]

    def list_runs(
        self,
        *,
        username: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit or 100), 1000))
        offset = max(0, int(offset or 0))
        with _lock:
            data = self._read()
            runs = list(data.get("runs") or [])

        # Newest first
        runs.reverse()
        if username:
            runs = [r for r in runs if (r.get("username") or "").lower() == username.lower()]
        if user_id:
            runs = [r for r in runs if r.get("user_id") == user_id]
        if status:
            runs = [r for r in runs if r.get("status") == status]
        if search and str(search).strip():
            needle = str(search).strip().lower()

            def _match(run: dict) -> bool:
                fields = (
                    run.get("prompt_id"),
                    run.get("id"),
                    run.get("username"),
                    run.get("user_id"),
                    run.get("workflow_name"),
                    run.get("status"),
                    run.get("started_at"),
                    run.get("finished_at"),
                    # Comfy-style aliases people may paste
                    f"job:{run.get('prompt_id')}" if run.get("prompt_id") else "",
                    f"job_id:{run.get('prompt_id')}" if run.get("prompt_id") else "",
                )
                hay = " ".join(str(x) for x in fields if x is not None).lower()
                return needle in hay

            runs = [r for r in runs if _match(r)]

        # Normalize job_id alias for API consumers (same as prompt_id)
        for r in runs:
            if "job_id" not in r:
                r["job_id"] = r.get("prompt_id")

        total = len(runs)
        page = runs[offset : offset + limit]
        return {
            "runs": page,
            "total": total,
            "limit": limit,
            "offset": offset,
            "search": (str(search).strip() if search else None),
        }

    def export_runs(
        self,
        *,
        username: str | None = None,
        user_id: str | None = None,
        search: str | None = None,
        status: str | None = None,
        limit: int = 50000,
    ) -> list[dict[str, Any]]:
        """All matching runs (newest first) for CSV/Excel export."""
        result = self.list_runs(
            username=username,
            user_id=user_id,
            search=search,
            status=status,
            limit=limit,
            offset=0,
        )
        return list(result.get("runs") or [])

    @staticmethod
    def runs_to_csv(runs: list[dict[str, Any]]) -> str:
        """Build CSV text (UTF-8 BOM friendly for Excel)."""
        import csv
        import io

        fields = [
            "started_at",
            "finished_at",
            "duration_sec",
            "username",
            "user_id",
            "job_id",
            "prompt_id",
            "workflow_name",
            "status",
            "node_count",
            "id",
        ]
        buf = io.StringIO()
        # Excel on Windows likes UTF-8 BOM
        buf.write("\ufeff")
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in runs:
            row = {k: r.get(k, "") for k in fields}
            if not row.get("job_id"):
                row["job_id"] = r.get("prompt_id") or ""
            writer.writerow(row)
        return buf.getvalue()

    @staticmethod
    def runs_to_xlsx_bytes(runs: list[dict[str, Any]]) -> bytes:
        """
        Build a minimal .xlsx workbook without third-party deps (Office Open XML).
        """
        import zipfile
        import io
        from xml.sax.saxutils import escape

        fields = [
            ("started_at", "Started (UTC)"),
            ("finished_at", "Finished (UTC)"),
            ("duration_sec", "Duration (sec)"),
            ("username", "Username"),
            ("user_id", "User ID"),
            ("job_id", "Job ID"),
            ("prompt_id", "Prompt ID"),
            ("workflow_name", "Workflow"),
            ("status", "Status"),
            ("node_count", "Nodes"),
            ("id", "Log ID"),
        ]

        def cell_xml(col_idx: int, row_idx: int, value) -> str:
            # A1-style ref
            col = ""
            n = col_idx
            while n:
                n, rem = divmod(n - 1, 26)
                col = chr(65 + rem) + col
            ref = f"{col}{row_idx}"
            text = "" if value is None else str(value)
            # inline string
            return (
                f'<c r="{ref}" t="inlineStr"><is><t>{escape(text)}</t></is></c>'
            )

        rows_xml = []
        # header
        header_cells = "".join(
            cell_xml(i + 1, 1, title) for i, (_, title) in enumerate(fields)
        )
        rows_xml.append(f'<row r="1">{header_cells}</row>')
        for r_i, run in enumerate(runs, start=2):
            cells = []
            for c_i, (key, _) in enumerate(fields):
                val = run.get(key, "")
                if key == "job_id" and not val:
                    val = run.get("prompt_id") or ""
                cells.append(cell_xml(c_i + 1, r_i, val))
            rows_xml.append(f'<row r="{r_i}">{"".join(cells)}</row>')

        sheet = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(rows_xml)}</sheetData></worksheet>'
        )
        workbook = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Run Log" sheetId="1" r:id="rId1"/></sheets></workbook>'
        )
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        )
        rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>'
        )
        wb_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>'
        )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types)
            zf.writestr("_rels/.rels", rels)
            zf.writestr("xl/workbook.xml", workbook)
            zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
            zf.writestr("xl/worksheets/sheet1.xml", sheet)
        return buf.getvalue()

    def stats(
        self,
        *,
        username: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        with _lock:
            data = self._read()
            runs = list(data.get("runs") or [])

        if username:
            runs = [r for r in runs if (r.get("username") or "").lower() == username.lower()]
        if user_id:
            runs = [r for r in runs if r.get("user_id") == user_id]

        by_user: dict[str, dict[str, Any]] = {}
        by_workflow: dict[str, int] = {}
        for r in runs:
            uname = r.get("username") or "guest"
            bucket = by_user.setdefault(
                uname,
                {
                    "username": uname,
                    "user_id": r.get("user_id"),
                    "total_runs": 0,
                    "completed": 0,
                    "error": 0,
                    "running": 0,
                    "queued": 0,
                    "last_run_at": None,
                    "workflows": {},
                },
            )
            bucket["total_runs"] += 1
            st = r.get("status") or "unknown"
            if st in bucket:
                bucket[st] += 1
            elif st == "completed":
                bucket["completed"] += 1
            elif st in ("error", "failed", "interrupted"):
                bucket["error"] += 1
            elif st == "running":
                bucket["running"] += 1
            elif st == "queued":
                bucket["queued"] += 1

            started = r.get("started_at")
            if started and (
                not bucket["last_run_at"] or started > bucket["last_run_at"]
            ):
                bucket["last_run_at"] = started

            wf = r.get("workflow_name") or "Unnamed workflow"
            bucket["workflows"][wf] = bucket["workflows"].get(wf, 0) + 1
            by_workflow[wf] = by_workflow.get(wf, 0) + 1

        # Convert per-user workflow maps to sorted lists
        users_out = []
        for bucket in by_user.values():
            wfs = bucket.pop("workflows", {})
            bucket["top_workflows"] = sorted(
                [{"name": k, "count": v} for k, v in wfs.items()],
                key=lambda x: x["count"],
                reverse=True,
            )[:20]
            users_out.append(bucket)
        users_out.sort(key=lambda x: x["total_runs"], reverse=True)

        top_workflows = sorted(
            [{"name": k, "count": v} for k, v in by_workflow.items()],
            key=lambda x: x["count"],
            reverse=True,
        )[:30]

        return {
            "total_runs": len(runs),
            "users": users_out,
            "top_workflows": top_workflows,
        }

    def clear(self, *, username: str | None = None) -> int:
        with _lock:
            data = self._read()
            runs = data.get("runs") or []
            if username:
                before = len(runs)
                data["runs"] = [
                    r
                    for r in runs
                    if (r.get("username") or "").lower() != username.lower()
                ]
                removed = before - len(data["runs"])
            else:
                removed = len(runs)
                data["runs"] = []
            self._write(data)
            return removed


# Lazy singleton bound from constants / access_control
_run_log: WorkflowRunLog | None = None


def get_run_log() -> WorkflowRunLog:
    global _run_log
    if _run_log is None:
        from ..constants import CURRENT_DIR

        path = os.path.join(CURRENT_DIR, "users", "workflow_runs.json")
        _run_log = WorkflowRunLog(path)
    return _run_log
