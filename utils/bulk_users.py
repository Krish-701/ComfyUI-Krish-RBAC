"""Bulk user import from CSV: name,email,password,role."""
from __future__ import annotations

import csv
import io
import re
import uuid
from typing import Any

ALLOWED_ROLES = {"admin", "power", "user", "guest"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")


def normalize_username(name: str) -> str:
    """Turn display/name field into a valid username (letters, numbers, _)."""
    raw = (name or "").strip()
    # spaces / dots / hyphens → underscore
    cleaned = re.sub(r"[\s.\-]+", "_", raw)
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", cleaned)
    return cleaned


def is_valid_email(email: str) -> bool:
    return bool(email and _EMAIL_RE.match(email.strip()))


def parse_csv_users(text: str) -> list[dict[str, str]]:
    """
    Parse CSV text into row dicts.

    Expected columns (header optional):
      name,email,password,role

    Also accepts aliases:
      name|username|user, email, password|pass, role|group|groups
    """
    if not text or not str(text).strip():
        return []

    # Strip BOM
    raw = str(text).lstrip("\ufeff").strip()
    # Normalize newlines
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    sample = raw[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(raw), dialect)
    rows = [r for r in reader if any(str(c).strip() for c in r)]
    if not rows:
        return []

    def _norm_header(h: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (h or "").strip().lower())

    first = [_norm_header(c) for c in rows[0]]
    # Map header tokens → field names.
    # Do NOT map bare "user" → name: role values are often "user" and would
    # falsely look like a header row (e.g. ...,user).
    header_aliases = {
        "name": "name",
        "username": "name",
        "email": "email",
        "mail": "email",
        "password": "password",
        "pass": "password",
        "pwd": "password",
        "role": "role",
        "group": "role",
        "groups": "role",
    }
    # Require at least 2 real header labels so data rows like
    # nkrishnan,...,user are not treated as headers.
    header_hits = sum(1 for h in first if h in header_aliases)
    has_header = header_hits >= 2

    parsed: list[dict[str, str]] = []
    if has_header:
        colmap: dict[int, str] = {}
        for i, h in enumerate(first):
            key = header_aliases.get(h)
            if key:
                colmap[i] = key
        data_rows = rows[1:]
        for row in data_rows:
            item = {"name": "", "email": "", "password": "", "role": "user"}
            for i, val in enumerate(row):
                key = colmap.get(i)
                if key:
                    item[key] = str(val).strip()
            if not item["role"]:
                item["role"] = "user"
            parsed.append(item)
    else:
        for row in rows:
            # Pad short rows
            cells = list(row) + [""] * max(0, 4 - len(row))
            name, email, password, role = cells[0], cells[1], cells[2], cells[3]
            parsed.append(
                {
                    "name": str(name).strip(),
                    "email": str(email).strip(),
                    "password": str(password).strip(),
                    "role": (str(role).strip() or "user"),
                }
            )
    return parsed


def import_users_from_rows(
    users_db,
    rows: list[dict[str, str]],
    *,
    sync_comfy=None,
    ensure_workflow_dir=None,
) -> dict[str, Any]:
    """
    Create users from parsed rows.

    Returns summary:
      { created: [...], skipped: [...], errors: [...], created_count, skipped_count, error_count }
    """
    created: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    for idx, row in enumerate(rows, start=1):
        name_raw = (row.get("name") or "").strip()
        email = (row.get("email") or "").strip().lower()
        password = row.get("password") or ""
        role = (row.get("role") or "user").strip().lower()

        line_ref = f"row {idx}"

        if not name_raw and not email and not password:
            continue

        username = normalize_username(name_raw)
        if not username or not _NAME_RE.match(username):
            errors.append(
                {
                    "line": line_ref,
                    "name": name_raw,
                    "email": email,
                    "error": "Invalid name/username (use letters, numbers, underscore; min 3 chars)",
                }
            )
            continue

        if not is_valid_email(email):
            errors.append(
                {
                    "line": line_ref,
                    "name": username,
                    "email": email,
                    "error": "Invalid or missing email",
                }
            )
            continue

        if not password or len(password) < 8:
            errors.append(
                {
                    "line": line_ref,
                    "name": username,
                    "email": email,
                    "error": "Password must be at least 8 characters",
                }
            )
            continue

        if role not in ALLOWED_ROLES:
            errors.append(
                {
                    "line": line_ref,
                    "name": username,
                    "email": email,
                    "error": f"Invalid role '{role}' (use admin, power, user, or guest)",
                }
            )
            continue

        # Duplicates
        existing_id, existing = users_db.get_user(username=username)
        if existing_id:
            skipped.append(
                {
                    "line": line_ref,
                    "name": username,
                    "email": email,
                    "reason": "username already exists",
                }
            )
            continue

        if users_db.email_exists(email):
            skipped.append(
                {
                    "line": line_ref,
                    "name": username,
                    "email": email,
                    "reason": "email already exists",
                }
            )
            continue

        # Email must not collide with another username either
        eid, _ = users_db.get_user(username=email)
        if eid:
            skipped.append(
                {
                    "line": line_ref,
                    "name": username,
                    "email": email,
                    "reason": "email conflicts with an existing username",
                }
            )
            continue

        try:
            new_id = str(uuid.uuid4())
            is_admin = role == "admin"
            users_db.add_user(
                new_id,
                username,
                password,
                is_admin,
                email=email,
                groups=[role],
            )
            if sync_comfy:
                try:
                    sync_comfy(new_id, username)
                except Exception:
                    pass
            if ensure_workflow_dir:
                try:
                    ensure_workflow_dir(username)
                except Exception:
                    pass
            created.append(
                {
                    "line": line_ref,
                    "name": username,
                    "email": email,
                    "role": role,
                }
            )
        except Exception as e:
            errors.append(
                {
                    "line": line_ref,
                    "name": username,
                    "email": email,
                    "error": str(e),
                }
            )

    return {
        "created": created,
        "skipped": skipped,
        "errors": errors,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "error_count": len(errors),
    }


def import_users_from_csv_text(
    users_db,
    text: str,
    *,
    sync_comfy=None,
    ensure_workflow_dir=None,
) -> dict[str, Any]:
    rows = parse_csv_users(text)
    if not rows:
        return {
            "created": [],
            "skipped": [],
            "errors": [{"line": "file", "error": "No valid CSV rows found"}],
            "created_count": 0,
            "skipped_count": 0,
            "error_count": 1,
        }
    result = import_users_from_rows(
        users_db,
        rows,
        sync_comfy=sync_comfy,
        ensure_workflow_dir=ensure_workflow_dir,
    )
    result["parsed_rows"] = len(rows)
    return result
