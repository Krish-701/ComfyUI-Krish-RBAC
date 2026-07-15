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
        })
    return web.json_response({"users": users_list})


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

    # NEW: optional SFW flag
    sfw_check = data.get("sfw_check", None)

    success = patch_user_group(target, groups, is_admin_flag, sfw_check)
    if success:
        logger.info(f"[Audit] user updated: target={target} by {_admin_username(request)}")
        return web.json_response({"status": "ok"})
    return web.Response(status=404)


@routes.put("/usgromana/api/users/{target_user}/password")
async def api_reset_user_password(request):
    """
    Admin-only: set / reset a user's password.
    Body: { "password": "NewPass123!" }
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

    uid, rec = users_db.get_user(username=target)
    if not uid or not rec:
        return web.json_response({"error": "User not found"}, status=404)

    ok = users_db.set_password(target, new_password)
    if not ok:
        return web.json_response({"error": "Failed to update password"}, status=500)

    logger.info(
        f"[Audit] password reset: target={target} by {_admin_username(request)}"
    )
    return web.json_response(
        {
            "status": "ok",
            "message": f"Password updated for {rec.get('username') or target}",
            "username": rec.get("username") or target,
        }
    )


@routes.delete("/usgromana/api/users/{target_user}")
async def api_delete_user_route(request):
    if not is_admin(request): return web.json_response({"error": "Admin only"}, status=403)
    target = request.match_info["target_user"]
    if target == "guest": return web.json_response({"error": "Cannot delete guest"}, status=400)
    
    result = delete_user_record(target)
    if result == "last_admin": return web.json_response({"error": "Cannot delete last admin"}, status=400)
    if result is False: return web.Response(status=404)
    logger.info(f"[Audit] user deleted: target={target} by {_admin_username(request)}")
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
    """Admin-only NSFW management endpoints."""
    if not is_admin(request):
        return web.json_response({"error": "Admin only"}, status=403)
    
    try:
        data = await request.json()
        action = data.get("action", "").strip()
        
        print(f"[Usgromana] NSFW management action: {action}")
        
        from ..utils.sfw_intercept.nsfw_guard import (
            scan_all_images_in_output_directory,
            fix_incorrectly_cached_tags,
            clear_all_nsfw_tags
        )
        
        # Run blocking operations in executor to avoid blocking the event loop
        import asyncio
        loop = asyncio.get_event_loop()
        
        if action == "scan_all":
            force_rescan = bool(data.get("force_rescan", False))
            print(f"[Usgromana] Starting scan_all (force_rescan={force_rescan}) in executor...")
            result = await loop.run_in_executor(
                None, 
                scan_all_images_in_output_directory, 
                force_rescan
            )
            print(f"[Usgromana] scan_all completed: {result}")
            return web.json_response({
                "status": "ok",
                "message": f"Scanned {result['scanned']} images. Found {result['nsfw_found']} NSFW images.",
                "stats": result
            })
        
        elif action == "fix_incorrect":
            print(f"[Usgromana] Starting fix_incorrect in executor...")
            fixed_count = await loop.run_in_executor(
                None,
                fix_incorrectly_cached_tags
            )
            print(f"[Usgromana] fix_incorrect completed: {fixed_count} fixed")
            return web.json_response({
                "status": "ok",
                "message": f"Fixed {fixed_count} incorrectly cached images.",
                "fixed_count": fixed_count
            })
        
        elif action == "clear_all_tags":
            print(f"[Usgromana] Starting clear_all_tags in executor...")
            cleared_count = await loop.run_in_executor(
                None,
                clear_all_nsfw_tags
            )
            print(f"[Usgromana] clear_all_tags completed: {cleared_count} cleared")
            return web.json_response({
                "status": "ok",
                "message": f"Cleared NSFW tags from {cleared_count} images.",
                "cleared_count": cleared_count
            })
        
        else:
            return web.json_response({"error": f"Unknown action: {action}"}, status=400)
    
    except Exception as e:
        import traceback
        print(f"[Usgromana] NSFW management error: {e}")
        traceback.print_exc()
        return web.json_response({"error": str(e)}, status=500)


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
    Admin/power see all; regular users see only their own.
    """
    username, user_id, admin, role, can_view_all = _caller_identity(request)
    if not username:
        return web.json_response({"error": "Authentication required"}, status=401)

    try:
        from ..globals import access_control

        active = access_control.get_active_runs_snapshot()
        if not can_view_all:
            active = [
                r
                for r in active
                if (r.get("username") or "").lower() == username.lower()
                or r.get("user_id") == user_id
            ]
        # Normalize job_id alias
        for r in active:
            if "job_id" not in r:
                r["job_id"] = r.get("prompt_id")
        return web.json_response(
            {
                "active": active,
                "count": len(active),
                "viewer": username,
                "role": role,
                "is_admin": admin,
                "can_view_all_runs": can_view_all,
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
        logger.info(
            f"[Audit] Workflow run log cleared by {_admin_username(request)} "
            f"(user={filter_user or 'ALL'}, removed={removed})"
        )
        return web.json_response({"status": "ok", "removed": removed})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)