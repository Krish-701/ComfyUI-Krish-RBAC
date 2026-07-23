# --- START OF FILE routes/auth.py ---
import os
import uuid
from aiohttp import web
from ..globals import routes, users_db, jwt_auth, logger, timeout
from ..constants import HTML_DIR, MAX_TOKEN_EXPIRE_MINUTES, TOKEN_EXPIRE_MINUTES
from ..utils.bootstrap import ensure_guest_user, ensure_groups_config
from ..utils.ip_filter import get_ip
from ..utils import user_env
from ..utils.comfy_user_bridge import sync_user_to_comfy_manager

@routes.get("/register")
async def get_register(request: web.Request) -> web.Response:
    path = os.path.join(HTML_DIR, "register.html")
    if not os.path.exists(path): return web.Response(text="register.html not found", status=404)
    with open(path, "r") as f: html_content = f.read()
    if not users_db.load_users():
        html_content = html_content.replace("{{ X-Admin-User }}", "true")
    else:
        html_content = html_content.replace("{{ X-Admin-User }}", "false")
    return web.Response(body=html_content, content_type="text/html")

@routes.post("/register")
async def post_register(request: web.Request) -> web.Response:
    sanitized_data = request.get("_sanitized_data", {})
    ip = get_ip(request)
    new_username = sanitized_data.get("new_user_username")
    new_password = sanitized_data.get("new_user_password")
    username = sanitized_data.get("username")
    password = sanitized_data.get("password")

    is_timed_out, failed_attempts, remaining_seconds = timeout.check_is_timed_out(ip)
    if is_timed_out:
        return web.json_response(
            {
                "error": "Too many failed attempts. Please wait.",
                "failed_attempts": failed_attempts,
                "remaining_seconds": remaining_seconds,
                "code": "RATE_LIMIT",
            },
            status=429,
        )

    admin_user = users_db.get_admin_user()
    is_first_admin = (admin_user[0] is None)

    if not is_first_admin:
        admin_id, _ = users_db.authenticate(username, password)
        if not admin_id:
            timeout.add_failed_attempt(ip)
            return web.json_response({"error": "Invalid admin credentials"}, status=403)

    if users_db.get_user(new_username)[0] is not None:
        return web.json_response({"error": "Username exists"}, status=400)

    new_email = (sanitized_data.get("new_user_email") or "").strip().lower() or None
    if new_email and users_db.email_exists(new_email):
        return web.json_response({"error": "Email already registered"}, status=400)

    new_user_id = str(uuid.uuid4())
    users_db.add_user(
        new_user_id,
        new_username,
        new_password,
        is_first_admin,
        email=new_email,
    )
    sync_user_to_comfy_manager(new_user_id, new_username)

    # Create directory immediately
    user_env.get_user_workflow_dir(new_username)

    if is_first_admin:
        ensure_groups_config()
        ensure_guest_user()

    logger.registration_success(ip, new_username, username if not is_first_admin else None)
    timeout.remove_failed_attempts(ip)
    return web.json_response({"message": "User registered"})

@routes.get("/login")
async def get_login(request: web.Request) -> web.Response:
    if not users_db.load_users(): return web.HTTPFound("/register")
    if jwt_auth.get_token_from_request(request): return web.HTTPFound("/logout")
    path = os.path.join(HTML_DIR, "login.html")
    return web.FileResponse(path) if os.path.exists(path) else web.Response(text="login.html not found", status=404)


@routes.get("/change_password")
async def get_change_password(request: web.Request) -> web.Response:
    path = os.path.join(HTML_DIR, "change_password.html")
    return (
        web.FileResponse(path)
        if os.path.exists(path)
        else web.Response(text="change_password.html not found", status=404)
    )

@routes.post("/login")
async def post_login(request: web.Request) -> web.Response:
    sanitized_data = request.get("_sanitized_data", {})
    ip = get_ip(request)

    # Server-side rate limit (in addition to client + IP blacklist)
    is_timed_out, failed_attempts, remaining_seconds = timeout.check_is_timed_out(ip)
    if is_timed_out:
        return web.json_response(
            {
                "error": "Too many failed attempts. Please wait.",
                "failed_attempts": failed_attempts,
                "remaining_seconds": remaining_seconds,
                "code": "RATE_LIMIT",
            },
            status=429,
        )
    
    if str(sanitized_data.get("guest_login", "false")).lower() == "true":
        ensure_guest_user()
        guest_id, guest_rec = users_db.get_user("guest")
        if not guest_id:
            return web.json_response({"error": "Guest disabled"}, status=500)
        if guest_rec.get("disabled"):
            return web.json_response({"error": "Guest account is disabled"}, status=403)
        
        user_env.get_user_workflow_dir("guest")
        
        # Single session: new guest login invalidates previous guest sessions
        token = jwt_auth.create_access_token(
            {"id": guest_id, "username": "guest"},
            single_session=True,
        )
        sync_user_to_comfy_manager(guest_id, "guest")
        resp = web.json_response({"message": "Guest login", "jwt_token": token})
        resp.set_cookie("jwt_token", token, httponly=True, samesite="Strict", path="/")
        logger.login_success(ip, "guest")
        timeout.remove_failed_attempts(ip)
        try:
            from ..utils.presence import touch
            touch("guest")
        except Exception:
            pass
        return resp

    login_id = sanitized_data.get("username")  # username OR email
    password = sanitized_data.get("password")

    user_id, user_rec = users_db.authenticate(login_id, password)
    if user_rec and user_rec.get("_disabled") and not user_id:
        timeout.add_failed_attempt(ip)
        return web.json_response(
            {"error": "Account disabled. Contact an administrator.", "code": "DISABLED"},
            status=403,
        )
    if user_id and user_rec:
        username = user_rec.get("username") or login_id

        user_env.get_user_workflow_dir(username)

        # Single session: this login ends any previous session for the same user
        token = jwt_auth.create_access_token(
            {"id": user_id, "username": username},
            single_session=True,
        )
        sync_user_to_comfy_manager(user_id, username)
        must_change = bool(user_rec.get("must_change_password"))
        resp = web.json_response(
            {
                "message": "Login successful",
                "jwt_token": token,
                "username": username,
                "email": user_rec.get("email"),
                "must_change_password": must_change,
            }
        )
        resp.set_cookie("jwt_token", token, httponly=True, samesite="Strict", path="/")
        logger.login_success(ip, username)
        timeout.remove_failed_attempts(ip)
        try:
            from ..utils.presence import touch
            touch(username)
            from ..utils.audit_log import audit
            audit("login", actor=username, ip=ip, detail="User logged in")
        except Exception:
            pass
        return resp

    timeout.add_failed_attempt(ip)
    fa = timeout.get_failed_attempts(ip)
    _, _, rem = timeout.check_is_timed_out(ip)
    return web.json_response(
        {
            "error": "Invalid credentials",
            "failed_attempts": fa,
            "remaining_seconds": rem or None,
        },
        status=401,
    )


@routes.post("/usgromana/api/change-password")
async def api_change_password(request: web.Request) -> web.Response:
    """Logged-in user changes own password (also clears must_change_password)."""
    token = jwt_auth.get_token_from_request(request)
    if not token:
        return web.json_response({"error": "Authentication required"}, status=401)
    try:
        payload = jwt_auth.decode_access_token(token)
        username = payload.get("username")
    except Exception:
        return web.json_response({"error": "Invalid token"}, status=401)

    try:
        data = await request.json()
    except Exception:
        data = request.get("_sanitized_data") or {}

    current = data.get("current_password") or data.get("old_password") or ""
    new_pw = data.get("new_password") or data.get("password")
    if new_pw is None:
        return web.json_response({"error": "Missing new_password"}, status=400)
    if not str(current):
        return web.json_response({"error": "Current password is required"}, status=400)

    # Always verify current password (including forced change after admin reset)
    uid, auth_rec = users_db.authenticate(username, current)
    if auth_rec and auth_rec.get("_disabled") and not uid:
        return web.json_response({"error": "Account disabled"}, status=403)
    if not uid:
        return web.json_response({"error": "Current password incorrect"}, status=403)

    ok = users_db.set_password(username, str(new_pw), force_change=False)
    if not ok:
        return web.json_response({"error": "Failed to update password"}, status=500)
    users_db.clear_must_change_password(username)
    try:
        from ..utils.audit_log import audit
        from ..utils.ip_filter import get_ip as _get_ip
        audit(
            "password_change_self",
            actor=username,
            target=username,
            ip=_get_ip(request),
            detail="User changed own password",
        )
    except Exception:
        pass
    return web.json_response({"status": "ok", "message": "Password updated"})

@routes.get("/generate_token")
async def get_generate_token(request: web.Request) -> web.Response:
    if not users_db.load_users():
        return web.HTTPFound("/register")
    if jwt_auth.get_token_from_request(request):
        return web.HTTPFound("/logout")
    path = os.path.join(HTML_DIR, "generate_token.html")
    return (
        web.FileResponse(path)
        if os.path.exists(path)
        else web.Response(text="generate_token.html not found", status=404)
    )


@routes.get("/usgromana/generate_token")
async def get_generate_token_alias(request: web.Request) -> web.Response:
    return web.HTTPFound("/generate_token")


@routes.post("/generate_token")
async def post_generate_token(request: web.Request) -> web.Response:
    sanitized_data = request.get("_sanitized_data", {})
    ip = get_ip(request)
    username = sanitized_data.get("username")
    password = sanitized_data.get("password")

    try:
        expire_hours = int(
            sanitized_data.get("expire_hours", TOKEN_EXPIRE_MINUTES / 60)
        )
    except (TypeError, ValueError):
        return web.json_response(
            {"error": "Expiration hours must be a number"},
            status=400,
        )

    max_expire_hours = MAX_TOKEN_EXPIRE_MINUTES / 60
    if expire_hours > max_expire_hours:
        return web.json_response(
            {
                "error": f"Expiration hours must be smaller than {max_expire_hours}"
            },
            status=400,
        )

    if not username or not password:
        return web.json_response(
            {
                "error": "Missing login credentials (email/username and password)",
            },
            status=400,
        )

    user_id, user_rec = users_db.authenticate(username, password)
    if user_id and user_rec:
        timeout.remove_failed_attempts(ip)
        resolved_username = user_rec.get("username") or username
        # API tokens do not kick web sessions (token_type=api, no single-session)
        token = jwt_auth.create_access_token(
            {
                "id": user_id,
                "username": resolved_username,
                "token_type": "api",
            },
            expire_minutes=expire_hours * 60,
            single_session=False,
        )
        secure_flag = request.headers.get("X-Forwarded-Proto", "http") == "https"
        response = web.json_response(
            {
                "message": "JWT Token successfully generated",
                "jwt_token": token,
                "username": resolved_username,
            }
        )
        response.set_cookie(
            "jwt_token",
            token,
            httponly=True,
            secure=secure_flag,
            samesite="Strict",
        )
        logger.generate_success(ip, resolved_username, expire_hours)
        return response

    logger.generate_attempt(ip, username, password, expire_hours)
    timeout.add_failed_attempt(ip)
    return web.json_response(
        {"error": "Invalid email/username or password"}, status=401
    )


@routes.get("/logout")
async def get_logout(request: web.Request) -> web.Response:
    # Clear this user's active session so the token cannot be reused
    try:
        token = jwt_auth.get_token_from_request(request)
        if token:
            payload = jwt_auth.decode_access_token(token)
            if str(payload.get("token_type") or "session").lower() != "api":
                jwt_auth.invalidate_user_session(payload.get("id"))
    except Exception:
        pass
    resp = web.HTTPFound("/login")
    resp.del_cookie("jwt_token", path="/")
    return resp