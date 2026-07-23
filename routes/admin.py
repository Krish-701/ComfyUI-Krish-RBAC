# --- START OF FILE routes/admin.py ---
from aiohttp import web
from ..globals import routes, jwt_auth, users_db, ip_filter, logger, timeout
from ..utils.runtime_config import (
    get_blacklist_after_attempts,
    set_blacklist_after_attempts,
)
from ..constants import GROUPS_CONFIG_FILE, DEFAULT_GROUP_CONFIG_PATH, WHITELIST_FILE, BLACKLIST_FILE, USERS_FILE
from ..utils.json_utils import load_json_file, save_json_file
from ..utils.admin_logic import patch_user_group, delete_user_record
from ..utils.bootstrap import load_default_groups
from ..utils.ui_defaults import (
    get_ui_defaults,
    set_assets_imports_visibility,
    ASSETS_VISIBILITY_MODES,
)

def _admin_username(request):
    """Return the username of the authenticated admin, or 'unknown' for audit log."""
    token = jwt_auth.get_token_from_request(request)
    if not token:
        return "unknown"
    try:
        p = jwt_auth.decode_access_token(token)
        return p.get("username", "unknown")
    except Exception:
        return "unknown"

def is_admin(request):
    token = jwt_auth.get_token_from_request(request)
    if not token: return False
    try:
        p = jwt_auth.decode_access_token(token)
        _, u = users_db.get_user(p['username'])
        return u.get('admin', False) or "admin" in u.get('groups', [])
    except Exception:
        return False


def is_power_or_admin(request):
    """Admin or power role — for dashboard / ops views."""
    if is_admin(request):
        return True
    token = jwt_auth.get_token_from_request(request)
    if not token:
        return False
    try:
        p = jwt_auth.decode_access_token(token)
        _, u = users_db.get_user(p["username"])
        if not u:
            return False
        groups = [str(g).lower() for g in (u.get("groups") or [])]
        return "power" in groups or "admin" in groups or bool(u.get("admin"))
    except Exception:
        return False

@routes.get("/usgromana/health")
async def health(request):
    """Readiness/health check for reverse proxies and load balancers. Returns 200 when extension is loaded and users DB is readable."""
    try:
        users_db.load_users()
        return web.json_response({"status": "ok", "extension": "usgromana"})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=503)

@routes.get("/usgromana/api/groups")
async def api_groups(request):
    default_cfg = load_default_groups()
    return web.json_response({"groups": load_json_file(GROUPS_CONFIG_FILE, default_cfg)})

@routes.put("/usgromana/api/groups")
async def api_update_groups(request):
    if not is_admin(request): return web.json_response({"error": "Admin only"}, status=403)
    try:
        data = await request.json()
        new_groups = data.get("groups", {})
        current = load_json_file(GROUPS_CONFIG_FILE, {})
        for g, perms in new_groups.items():
            g_lower = g.lower()
            if g_lower not in current: current[g_lower] = {}
            for k, v in perms.items():
                current[g_lower][k] = bool(v)
        save_json_file(GROUPS_CONFIG_FILE, current)
        logger.info(f"[Audit] groups updated by {_admin_username(request)}")
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

@routes.get("/usgromana/api/ui-defaults")
async def api_ui_defaults_get(request):
    if not is_admin(request):
        return web.json_response({"error": "Admin only"}, status=403)
    defaults = get_ui_defaults()
    return web.json_response(
        {
            "defaults": defaults,
            "assets_imports_visibility": defaults.get("assets_imports_visibility"),
            "allowed_assets_imports_visibility": list(ASSETS_VISIBILITY_MODES),
        }
    )


@routes.put("/usgromana/api/ui-defaults")
async def api_ui_defaults_put(request):
    if not is_admin(request):
        return web.json_response({"error": "Admin only"}, status=403)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    mode = (data.get("assets_imports_visibility") or "").strip()
    if not mode:
        return web.json_response(
            {"error": "Missing assets_imports_visibility"}, status=400
        )
    try:
        set_assets_imports_visibility(mode)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    logger.info(
        f"[Audit] UI defaults updated (assets_imports_visibility={mode}) "
        f"by {_admin_username(request)}"
    )
    defaults = get_ui_defaults()
    return web.json_response(
        {
            "status": "ok",
            "defaults": defaults,
            "assets_imports_visibility": defaults.get("assets_imports_visibility"),
        }
    )


@routes.get("/usgromana/api/users")
async def api_users(request):
    # Security: You might want to restrict this to admins only too
    if not is_admin(request): return web.json_response({"error": "Admin only"}, status=403)
    
    raw = load_json_file(USERS_FILE, {})
    users_list = []
    iterable = raw.get("users", raw).values() if isinstance(raw.get("users", raw), dict) else raw.get("users", raw)
    for u in iterable:
        users_list.append({
            "username": u.get("username", "unknown"),
            "email": u.get("email") or "",
            "groups": [g.lower() for g in u.get("groups", ["user"])],
            "is_admin": u.get("admin", False),
            # NEW: per-user SFW flag; default = True (SFW enabled)
            "sfw_check": u.get("sfw_check", True),
            "disabled": bool(u.get("disabled")),
            "must_change_password": bool(u.get("must_change_password")),
            "created_at": u.get("created_at") or "",
        })
    return web.json_response({"users": users_list})


@routes.get("/usgromana/api/users/export")
async def api_users_export(request):
    """
    Admin-only: export users as CSV.

    Columns: name,email,password,role
    - name = username
    - email = login email
    - password = stored hash if present (plain passwords are never stored; use Reset PW to set new ones)
    - role = primary group

    Query: format=csv (default)
    """
    if not is_admin(request):
        return web.json_response({"error": "Admin only"}, status=403)

    try:
        import csv
        import io
        from datetime import datetime, timezone

        raw = load_json_file(USERS_FILE, {})
        data = raw.get("users", raw) if isinstance(raw, dict) else raw
        if not isinstance(data, dict):
            data = {}

        # Optional: include password hashes (default true for backup; still not plaintext)
        include_hash = (request.rel_url.query.get("include_password") or "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )

        buf = io.StringIO()
        buf.write("\ufeff")  # Excel-friendly UTF-8 BOM
        writer = csv.writer(buf)
        writer.writerow(["name", "email", "password", "role"])

        for uid, u in data.items():
            if not isinstance(u, dict):
                continue
            name = u.get("username") or ""
            email = u.get("email") or ""
            groups = u.get("groups") or []
            if isinstance(groups, str):
                groups = [groups]
            role = (groups[0] if groups else ("admin" if u.get("admin") else "user")).lower()
            # Plaintext passwords cannot be exported — only bcrypt hash exists on disk
            pw = (u.get("password") or "") if include_hash else ""
            writer.writerow([name, email, pw, role])

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        csv_text = buf.getvalue()
        logger.info(f"[Audit] users CSV export by {_admin_username(request)}")
        # aiohttp: charset must not be embedded in content_type
        return web.Response(
            text=csv_text,
            content_type="text/csv",
            charset="utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="users_export_{stamp}.csv"',
            },
        )
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.post("/usgromana/api/users/bulk")
async def api_users_bulk_import(request):
    """
    Admin bulk user import from CSV.

    CSV format (header optional):
      name,email,password,role
    Example:
      nkrishnan,nkrishnan@pixstone.com,Nkri@Sh12,user

    Body options:
      - multipart/form-data with field ``file`` (CSV upload)
      - application/json: { "csv": "..." } or { "text": "..." }
      - text/csv or text/plain raw body
    """
    if not is_admin(request):
        return web.json_response({"error": "Admin only"}, status=403)

    try:
        from ..utils.bulk_users import import_users_from_csv_text
        from ..utils.comfy_user_bridge import sync_user_to_comfy_manager
        from ..utils import user_env

        csv_text = ""
        ctype = (request.content_type or "").lower()

        if "multipart" in ctype or "form-data" in ctype:
            reader = await request.multipart()
            while True:
                part = await reader.next()
                if part is None:
                    break
                # Prefer file field named file/csv, else any file, else text fields
                if part.filename:
                    raw = await part.read(decode=False)
                    csv_text = raw.decode("utf-8-sig", errors="replace")
                    break
                name = (part.name or "").lower()
                if name in ("csv", "text", "content", "file"):
                    csv_text = await part.text()
                    if csv_text:
                        break
        elif "json" in ctype:
            data = await request.json()
            csv_text = data.get("csv") or data.get("text") or data.get("content") or ""
            if not csv_text and isinstance(data.get("rows"), list):
                # Allow pre-parsed rows as JSON
                from ..utils.bulk_users import import_users_from_rows

                result = import_users_from_rows(
                    users_db,
                    data["rows"],
                    sync_comfy=sync_user_to_comfy_manager,
                    ensure_workflow_dir=user_env.get_user_workflow_dir,
                )
                logger.info(
                    f"[Audit] bulk user import (json rows) by {_admin_username(request)}: "
                    f"created={result.get('created_count')} skipped={result.get('skipped_count')} "
                    f"errors={result.get('error_count')}"
                )
                return web.json_response({"status": "ok", **result})
        else:
            csv_text = await request.text()

        if not str(csv_text).strip():
            return web.json_response(
                {
                    "error": "No CSV content provided",
                    "format": "name,email,password,role",
                    "example": "nkrishnan,nkrishnan@pixstone.com,Nkri@Sh12,user",
                },
                status=400,
            )

        result = import_users_from_csv_text(
            users_db,
            csv_text,
            sync_comfy=sync_user_to_comfy_manager,
            ensure_workflow_dir=user_env.get_user_workflow_dir,
        )
        # Force in-memory DB reload after bulk file writes
        users_db.load_users()
        logger.info(
            f"[Audit] bulk user import by {_admin_username(request)}: "
            f"created={result.get('created_count')} skipped={result.get('skipped_count')} "
            f"errors={result.get('error_count')}"
        )
        return web.json_response({"status": "ok", **result})
    except Exception as e:
        import traceback

        traceback.print_exc()
        return web.json_response({"error": str(e)}, status=500)


@routes.put("/usgromana/api/users/{target_user}")
async def api_update_user_route(request):
    if not is_admin(request):
        return web.json_response({"error": "Admin only"}, status=403)

    target = request.match_info["target_user"]
    data = await request.json()

    groups = [g.lower() for g in data.get("groups", [])]
    is_admin_flag = "admin" in groups

    # Hardcoded system admin "Krish" is always admin — role cannot be changed
    try:
        from ..utils.bootstrap import DEFAULT_ADMIN_USERNAME
        locked = (DEFAULT_ADMIN_USERNAME or "Krish").lower()
    except Exception:
        locked = "krish"
    if str(target or "").lower() == locked:
        if groups and "admin" not in groups:
            return web.json_response(
                {"error": f"Cannot change role of system admin {target}; always admin."},
                status=400,
            )
        groups = ["admin"]
        is_admin_flag = True

    # NEW: optional SFW flag
    sfw_check = data.get("sfw_check", None)

    success = patch_user_group(target, groups, is_admin_flag, sfw_check)
    if success:
        actor = _admin_username(request)
        logger.info(f"[Audit] user updated: target={target} by {actor}")
        try:
            from ..utils.audit_log import audit
            from ..utils.ip_filter import get_ip
            audit(
                "user_role_update",
                actor=actor,
                target=target,
                detail=f"groups={groups} sfw={sfw_check}",
                meta={"groups": groups, "sfw_check": sfw_check},
                ip=get_ip(request),
            )
        except Exception:
            pass
        return web.json_response({"status": "ok"})
    return web.Response(status=404)


@routes.put("/usgromana/api/users/{target_user}/password")
async def api_reset_user_password(request):
    """
    Admin-only: set / reset a user's password.
    Body: { "password": "...", "force_change": false }
    force_change defaults to false so the user can log in with the shared password
    without being forced to pick a new one.
    """
    if not is_admin(request):
        return web.json_response({"error": "Admin only"}, status=403)

    target = request.match_info["target_user"]
    if not target or target.lower() == "guest":
        return web.json_response(
            {"error": "Cannot reset password for guest"}, status=400
        )

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # Admin reset: no length / complexity rules — any password string is allowed.
    if "password" not in data and "new_password" not in data:
        return web.json_response({"error": "Missing 'password' field"}, status=400)
    new_password = data.get("password")
    if new_password is None:
        new_password = data.get("new_password")
    if not isinstance(new_password, str):
        new_password = str(new_password)

    # Default false: share password with user; they log in as-is
    force_change = data.get("force_change", False)
    if isinstance(force_change, str):
        force_change = force_change.strip().lower() in ("1", "true", "yes")

    uid, rec = users_db.get_user(username=target)
    if not uid or not rec:
        return web.json_response({"error": "User not found"}, status=404)

    ok = users_db.set_password(target, new_password, force_change=bool(force_change))
    if not ok:
        return web.json_response({"error": "Failed to update password"}, status=500)
    # Always clear must_change when force_change is false (covers prior forced flags)
    if not force_change:
        try:
            users_db.clear_must_change_password(target)
        except Exception:
            pass

    actor = _admin_username(request)
    logger.info(f"[Audit] password reset: target={target} by {actor}")
    try:
        from ..utils.audit_log import audit
        from ..utils.ip_filter import get_ip
        audit(
            "password_reset",
            actor=actor,
            target=target,
            detail=f"force_change={bool(force_change)}",
            meta={"force_change": bool(force_change)},
            ip=get_ip(request),
        )
    except Exception:
        pass
    return web.json_response(
        {
            "status": "ok",
            "message": f"Password updated for {rec.get('username') or target}",
            "username": rec.get("username") or target,
            "force_change": bool(force_change),
        }
    )


@routes.put("/usgromana/api/users/{target_user}/disabled")
async def api_set_user_disabled(request):
    """Admin-only soft ban: disable/enable account without deleting."""
    if not is_admin(request):
        return web.json_response({"error": "Admin only"}, status=403)
    target = request.match_info["target_user"]
    if not target or target.lower() == "guest":
        return web.json_response({"error": "Cannot disable guest this way"}, status=400)
    if target == _admin_username(request):
        return web.json_response({"error": "Cannot disable yourself"}, status=400)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    disabled = bool(data.get("disabled", True))
    ok = users_db.set_disabled(target, disabled)
    if not ok:
        return web.json_response({"error": "User not found or cannot disable"}, status=404)
    actor = _admin_username(request)
    try:
        from ..utils.audit_log import audit
        from ..utils.ip_filter import get_ip
        audit(
            "user_disable" if disabled else "user_enable",
            actor=actor,
            target=target,
            detail="disabled" if disabled else "enabled",
            ip=get_ip(request),
        )
    except Exception:
        pass
    logger.info(f"[Audit] user {'disabled' if disabled else 'enabled'}: {target} by {actor}")
    return web.json_response({"status": "ok", "username": target, "disabled": disabled})


@routes.delete("/usgromana/api/users/{target_user}")
async def api_delete_user_route(request):
    if not is_admin(request): return web.json_response({"error": "Admin only"}, status=403)
    target = request.match_info["target_user"]
    if target == "guest": return web.json_response({"error": "Cannot delete guest"}, status=400)
    
    result = delete_user_record(target)
    if result == "last_admin": return web.json_response({"error": "Cannot delete last admin"}, status=400)
    if result is False: return web.Response(status=404)
    actor = _admin_username(request)
    logger.info(f"[Audit] user deleted: target={target} by {actor}")
    try:
        from ..utils.audit_log import audit
        from ..utils.ip_filter import get_ip
        audit("user_delete", actor=actor, target=target, ip=get_ip(request), detail="User deleted")
    except Exception:
        pass
    return web.json_response({"status": "ok"})

@routes.get("/usgromana/api/ip-lists")
async def api_ip_lists(request):
    whitelist, blacklist = ip_filter.load_filter_list()
    return web.json_response({
        "whitelist": [str(ip) for ip in (whitelist or [])],
        "blacklist": [str(ip) for ip in (blacklist or [])],
        "blacklist_after_attempts": get_blacklist_after_attempts(),
    })

@routes.put("/usgromana/api/ip-lists")
async def api_update_ip_lists(request):
    if not is_admin(request): 
        return web.json_response({"error": "Admin only"}, status=403)
    try:
        data = await request.json()
        whitelist = data.get("whitelist", [])
        blacklist = data.get("blacklist", [])
        
        # Validate and write whitelist
        import ipaddress
        
        # Write whitelist
        with open(WHITELIST_FILE, "w") as f:
            for ip_entry in whitelist:
                ip_entry = ip_entry.strip()
                if ip_entry:
                    try:
                        # Validate IP or CIDR
                        try:
                            ipaddress.ip_address(ip_entry)
                        except ValueError:
                            ipaddress.ip_network(ip_entry, strict=False)
                        f.write(ip_entry + "\n")
                    except ValueError:
                        # Skip invalid entries
                        continue
        
        # Write blacklist
        with open(BLACKLIST_FILE, "w") as f:
            for ip_entry in blacklist:
                ip_entry = ip_entry.strip()
                if ip_entry:
                    try:
                        # Validate IP or CIDR
                        try:
                            ipaddress.ip_address(ip_entry)
                        except ValueError:
                            ipaddress.ip_network(ip_entry, strict=False)
                        f.write(ip_entry + "\n")
                    except ValueError:
                        # Skip invalid entries
                        continue
        
        # Reload the filter lists to update in-memory cache
        ip_filter.load_filter_list()

        blacklist_after_attempts = data.get("blacklist_after_attempts")
        if blacklist_after_attempts is not None:
            try:
                attempts = int(blacklist_after_attempts)
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": "blacklist_after_attempts must be a non-negative integer"},
                    status=400,
                )
            if attempts < 0:
                return web.json_response(
                    {"error": "blacklist_after_attempts must be a non-negative integer"},
                    status=400,
                )
            normalized = set_blacklist_after_attempts(attempts)
            timeout.blacklist_after_attempts = normalized

        logger.info(f"[Audit] IP lists updated by {_admin_username(request)}")
        return web.json_response(
            {
                "status": "ok",
                "blacklist_after_attempts": get_blacklist_after_attempts(),
            }
        )
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

@routes.post("/usgromana/api/nsfw-management")
async def api_nsfw_management(request):
    # NSFW management UI removed — endpoint kept as soft-disabled for old clients
    if not is_admin(request):
        return web.json_response({"error": "Admin only"}, status=403)
    return web.json_response(
        {
            "error": "NSFW Management has been removed from Krish RBAC.",
            "removed": True,
        },
        status=410,
    )


# ---------------------------------------------------------------------------
# Workflow run log: who ran what, when, how many times
# ---------------------------------------------------------------------------

def _caller_identity(request):
    """
    Return (username, user_id, is_admin, role, can_view_all_runs).

    can_view_all_runs: admin + power can see every user's activity.
    Regular user/guest only see their own runs.
    """
    token = jwt_auth.get_token_from_request(request)
    if not token:
        return None, None, False, "guest", False
    try:
        p = jwt_auth.decode_access_token(token)
        username = p.get("username")
        user_id = p.get("id")
        _, u = users_db.get_user(username=username) if username else (None, {})
        groups = [g.lower() for g in (u.get("groups") or [])] if u else []
        admin = bool(u and (u.get("admin") or "admin" in groups))
        role = "guest"
        for candidate in ("admin", "power", "user", "guest"):
            if candidate in groups or (candidate == "admin" and admin):
                role = candidate
                break
        can_view_all = admin or role in ("admin", "power")
        return username, user_id, admin, role, can_view_all
    except Exception:
        return None, None, False, "guest", False


@routes.get("/usgromana/api/workflow-runs")
async def api_workflow_runs(request):
    """
    List workflow execution history.

    - Admin / power: all users (optional ?user= filter)
    - user / guest: only their own runs
    Query: limit, offset, user, status, q (search job id / name / workflow / status)
    """
    username, user_id, admin, role, can_view_all = _caller_identity(request)
    if not username:
        return web.json_response({"error": "Authentication required"}, status=401)

    try:
        from ..utils.workflow_run_log import get_run_log

        q = request.rel_url.query
        limit = int(q.get("limit", "100"))
        offset = int(q.get("offset", "0"))
        status = q.get("status") or None
        search = (q.get("q") or q.get("search") or "").strip() or None
        filter_user = (q.get("user") or "").strip() or None

        if not can_view_all:
            # Hard isolation: non-privileged users never query other accounts
            filter_user = username

        result = get_run_log().list_runs(
            username=filter_user,
            limit=limit,
            offset=offset,
            status=status,
            search=search,
        )
        result["viewer"] = username
        result["role"] = role
        result["is_admin"] = admin
        result["can_view_all_runs"] = can_view_all
        return web.json_response(result)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/usgromana/api/workflow-runs/stats")
async def api_workflow_runs_stats(request):
    """
    Aggregated stats: how many workflows each user ran, top workflows, last run time.
    Admin/power see everyone; regular users see only themselves.
    """
    username, user_id, admin, role, can_view_all = _caller_identity(request)
    if not username:
        return web.json_response({"error": "Authentication required"}, status=401)

    try:
        from ..utils.workflow_run_log import get_run_log

        filter_user = None if can_view_all else username
        # Privileged roles may filter to one user via ?user=
        q_user = (request.rel_url.query.get("user") or "").strip()
        if can_view_all and q_user:
            filter_user = q_user

        stats = get_run_log().stats(username=filter_user)
        stats["viewer"] = username
        stats["role"] = role
        stats["is_admin"] = admin
        stats["can_view_all_runs"] = can_view_all
        return web.json_response(stats)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/usgromana/api/workflow-runs/active")
async def api_workflow_runs_active(request):
    """
    Currently queued/running prompts with runner username, workflow, job id.

    Isolation:
      - admin / power: full server queue + can cancel
      - user / guest: **only their own jobs** (no other users' jobs/temp/output)
    """
    username, user_id, admin, role, can_view_all = _caller_identity(request)
    if not username:
        return web.json_response({"error": "Authentication required"}, status=401)

    try:
        from ..globals import access_control

        active = access_control.get_active_runs_snapshot()

        if not can_view_all:
            # Hard isolation: never leak other users' jobs to normal accounts
            uname = (username or "").lower()
            filtered = []
            for r in active:
                if access_control._same_queue_user(r.get("user_id"), user_id):
                    filtered.append(r)
                    continue
                if (r.get("username") or "").lower() == uname:
                    filtered.append(r)
                    continue
                if access_control._same_queue_user(r.get("username"), username):
                    filtered.append(r)
            active = filtered

        # Normalize job_id alias
        for r in active:
            if "job_id" not in r:
                r["job_id"] = r.get("prompt_id")

        can_cancel = bool(can_view_all)
        return web.json_response(
            {
                "active": active,
                "count": len(active),
                "viewer": username,
                "role": role,
                "is_admin": admin,
                "can_view_all_runs": can_view_all,
                "can_cancel": can_cancel,
            }
        )
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.delete("/usgromana/api/workflow-runs")
async def api_workflow_runs_clear(request):
    """Admin-only: clear run history (optional ?user= to clear one user)."""
    if not is_admin(request):
        return web.json_response({"error": "Admin only"}, status=403)
    try:
        from ..utils.workflow_run_log import get_run_log

        filter_user = (request.rel_url.query.get("user") or "").strip() or None
        removed = get_run_log().clear(username=filter_user)
        actor = _admin_username(request)
        logger.info(
            f"[Audit] Workflow run log cleared by {actor} "
            f"(user={filter_user or 'ALL'}, removed={removed})"
        )
        try:
            from ..utils.audit_log import audit
            from ..utils.ip_filter import get_ip
            audit(
                "run_log_clear",
                actor=actor,
                target=filter_user or "ALL",
                detail=f"removed={removed}",
                ip=get_ip(request),
            )
        except Exception:
            pass
        return web.json_response({"status": "ok", "removed": removed})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/usgromana/api/workflow-runs/export")
async def api_workflow_runs_export(request):
    """
    Export run log as CSV or Excel (.xlsx).

    Query:
      format=csv|xlsx|excel  (default csv)
      user=  (admin/power filter)
      q=     (search)
      status=
    """
    username, user_id, admin, role, can_view_all = _caller_identity(request)
    if not username:
        return web.json_response({"error": "Authentication required"}, status=401)

    try:
        from ..utils.workflow_run_log import get_run_log
        from datetime import datetime, timezone

        q = request.rel_url.query
        fmt = (q.get("format") or "csv").strip().lower()
        if fmt in ("excel", "xls"):
            fmt = "xlsx"
        search = (q.get("q") or q.get("search") or "").strip() or None
        status = (q.get("status") or "").strip() or None
        filter_user = (q.get("user") or "").strip() or None
        if not can_view_all:
            filter_user = username

        runs = get_run_log().export_runs(
            username=filter_user,
            search=search,
            status=status,
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        base_name = f"workflow_run_log_{stamp}"

        if fmt == "xlsx":
            data = get_run_log().runs_to_xlsx_bytes(runs)
            return web.Response(
                body=data,
                content_type=(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
                headers={
                    "Content-Disposition": f'attachment; filename="{base_name}.xlsx"',
                },
            )

        # Default CSV (Excel-friendly UTF-8 BOM)
        from ..utils.workflow_run_log import WorkflowRunLog

        csv_text = WorkflowRunLog.runs_to_csv(runs)
        # aiohttp: charset must not be embedded in content_type
        return web.Response(
            text=csv_text,
            content_type="text/csv",
            charset="utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{base_name}.csv"',
            },
        )
    except Exception as e:
        import traceback

        traceback.print_exc()
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/usgromana/api/queue-status")
async def api_queue_status(request):
    """
    Current user's queue occupancy and waiting number.
    Also returns server queue length.
    """
    username, user_id, admin, role, can_view_all = _caller_identity(request)
    if not username:
        return web.json_response({"error": "Authentication required"}, status=401)

    try:
        from ..globals import access_control
        from ..utils.presence import touch

        touch(username)
        status = access_control.get_user_queue_status(user_id)
        status["viewer"] = username
        status["role"] = role
        status["is_admin"] = admin
        status["can_view_all_runs"] = can_view_all
        return web.json_response(status)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.post("/usgromana/api/queue/cancel")
async def api_queue_cancel(request):
    """
    Cancel a pending or running job by prompt_id.
    Admin/power can cancel any user's job; others only their own.
    """
    username, user_id, admin, role, can_view_all = _caller_identity(request)
    if not username:
        return web.json_response({"error": "Authentication required"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    prompt_id = data.get("prompt_id") or data.get("job_id")
    if not prompt_id:
        return web.json_response({"error": "Missing prompt_id"}, status=400)

    try:
        from ..globals import access_control
        from ..utils.audit_log import audit
        from ..utils.ip_filter import get_ip

        # Bind identity for ownership checks; prefer username for folder keys
        bind_key = username or user_id
        if bind_key:
            access_control.set_current_user_id(bind_key, set_fallback=False)

        # Admin/power always privileged (re-check via access_control too)
        privileged = bool(can_view_all) or access_control.user_can_view_all(
            user_id
        ) or access_control.user_can_view_all(username)

        result = access_control.cancel_job_by_prompt_id(
            str(prompt_id), actor_can_view_all=privileged
        )
        if not result.get("ok"):
            status = 404 if result.get("code") == "NOT_FOUND" else 403
            return web.json_response(result, status=status)

        audit(
            "queue_cancel",
            actor=username,
            target=result.get("username") or "",
            detail=(
                f"cancelled {result.get('cancelled')} job {prompt_id} "
                f"({result.get('workflow_name')})"
            ),
            meta=result,
            ip=get_ip(request),
        )
        logger.info(
            f"[Audit] queue cancel: prompt_id={prompt_id} target={result.get('username')} "
            f"by {username} ({result.get('cancelled')})"
        )
        return web.json_response({"status": "ok", **result})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/usgromana/api/audit-log")
async def api_audit_log(request):
    """Admin-only structured audit log."""
    if not is_admin(request):
        return web.json_response({"error": "Admin only"}, status=403)
    try:
        from ..utils.audit_log import get_audit_log

        q = request.rel_url.query
        result = get_audit_log().list_entries(
            limit=int(q.get("limit", "200")),
            offset=int(q.get("offset", "0")),
            action=(q.get("action") or "").strip() or None,
            actor=(q.get("actor") or "").strip() or None,
            search=(q.get("q") or q.get("search") or "").strip() or None,
        )
        return web.json_response(result)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/usgromana/api/audit-log/export")
async def api_audit_log_export(request):
    """Admin-only CSV export of audit log."""
    if not is_admin(request):
        return web.json_response({"error": "Admin only"}, status=403)
    try:
        from ..utils.audit_log import get_audit_log
        from datetime import datetime, timezone

        q = request.rel_url.query
        csv_text = get_audit_log().export_csv(
            action=(q.get("action") or "").strip() or None,
            actor=(q.get("actor") or "").strip() or None,
            search=(q.get("q") or "").strip() or None,
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        # aiohttp: charset must not be embedded in content_type
        return web.Response(
            text=csv_text,
            content_type="text/csv",
            charset="utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="audit_log_{stamp}.csv"',
            },
        )
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/usgromana/api/dashboard")
async def api_dashboard(request):
    """
    Admin/power dashboard stats: online users, queue length, jobs/hour, top users.
    """
    if not is_power_or_admin(request):
        return web.json_response({"error": "Admin or power only"}, status=403)
    try:
        from ..globals import access_control
        from ..utils.presence import list_online
        from ..utils.workflow_run_log import get_run_log
        import time

        online = list_online()
        active = access_control.get_active_runs_snapshot()
        running = [a for a in active if a.get("status") == "running"]
        pending = [a for a in active if a.get("status") == "queued"]

        # Jobs in last hour from run log
        now = time.time()
        hour_ago = now - 3600
        runs = get_run_log().export_runs(limit=5000)
        last_hour = [
            r
            for r in runs
            if isinstance(r.get("started_ts"), (int, float)) and r["started_ts"] >= hour_ago
        ]
        # Fallback if no started_ts
        if not last_hour:
            last_hour = [r for r in runs[:50]]  # approximate

        by_user: dict[str, int] = {}
        for r in last_hour:
            u = r.get("username") or "unknown"
            by_user[u] = by_user.get(u, 0) + 1
        top_users = sorted(
            [{"username": k, "jobs": v} for k, v in by_user.items()],
            key=lambda x: x["jobs"],
            reverse=True,
        )[:15]

        stats = get_run_log().stats()

        return web.json_response(
            {
                "online_users": online,
                "online_count": len(online),
                "queue_length": len(active),
                "running": len(running),
                "pending": len(pending),
                "active_jobs": active,
                "jobs_last_hour": len(last_hour),
                "top_users_hour": top_users,
                "total_runs_all_time": stats.get("total_runs", 0),
                "users_all_time": stats.get("users") or [],
            }
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return web.json_response({"error": str(e)}, status=500)