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

    admin_user = users_db.get_admin_user()
    is_first_admin = (admin_user[0] is None)

    if not is_first_admin:
        if not users_db.check_username_password(username, password):
            timeout.add_failed_attempt(ip)
            return web.json_response({"error": "Invalid admin credentials"}, status=403)

    if None not in users_db.get_user(new_username):
        return web.json_response({"error": "Username exists"}, status=400)

    new_user_id = str(uuid.uuid4())
    users_db.add_user(new_user_id, new_username, new_password, is_first_admin)
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

@routes.post("/login")
async def post_login(request: web.Request) -> web.Response:
    sanitized_data = request.get("_sanitized_data", {})
    ip = get_ip(request)
    
    if str(sanitized_data.get("guest_login", "false")).lower() == "true":
        ensure_guest_user()
        guest_id, _ = users_db.get_user("guest")
        if not guest_id: return web.json_response({"error": "Guest disabled"}, status=500)
        
        user_env.get_user_workflow_dir("guest")
        
        token = jwt_auth.create_access_token({"id": guest_id, "username": "guest"})
        sync_user_to_comfy_manager(guest_id, "guest")
        resp = web.json_response({"message": "Guest login", "jwt_token": token})
        resp.set_cookie("jwt_token", token, httponly=True, samesite="Strict")
        logger.login_success(ip, "guest")
        timeout.remove_failed_attempts(ip)
        return resp

    username = sanitized_data.get("username")
    password = sanitized_data.get("password")

    if users_db.check_username_password(username, password):
        user_id, _ = users_db.get_user(username)
        
        user_env.get_user_workflow_dir(username)
        
        token = jwt_auth.create_access_token({"id": user_id, "username": username})
        sync_user_to_comfy_manager(user_id, username)
        resp = web.json_response({"message": "Login successful", "jwt_token": token})
        resp.set_cookie("jwt_token", token, httponly=True, samesite="Strict")
        logger.login_success(ip, username)
        timeout.remove_failed_attempts(ip)
        return resp

    timeout.add_failed_attempt(ip)
    return web.json_response({"error": "Invalid credentials"}, status=401)

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
            {"error": "Missing login credentials (username and password)"},
            status=400,
        )

    if users_db.check_username_password(username, password):
        timeout.remove_failed_attempts(ip)

        user_id, _ = users_db.get_user(username)
        token = jwt_auth.create_access_token(
            {"id": user_id, "username": username},
            expire_minutes=expire_hours * 60,
        )
        secure_flag = request.headers.get("X-Forwarded-Proto", "http") == "https"
        response = web.json_response(
            {
                "message": "JWT Token successfully generated",
                "jwt_token": token,
            }
        )
        response.set_cookie(
            "jwt_token",
            token,
            httponly=True,
            secure=secure_flag,
            samesite="Strict",
        )
        logger.generate_success(ip, username, expire_hours)
        return response

    logger.generate_attempt(ip, username, password, expire_hours)
    timeout.add_failed_attempt(ip)
    return web.json_response({"error": "Invalid username or password"}, status=401)


@routes.get("/logout")
async def get_logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/login")
    resp.del_cookie("jwt_token", path="/")
    return resp