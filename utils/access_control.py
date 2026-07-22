# --- START OF FILE utils/access_control.py ---
import os
import json
import heapq
import copy
import contextvars
from aiohttp import web
import folder_paths
from server import PromptServer
from execution import PromptQueue, MAXIMUM_HISTORY_SIZE
from .users_db import UsersDB

# Map Permission Keys -> URL Paths to Block
EXTENSION_BLOCK_MAP = {
    "settings_itools": ["/extensions/ComfyUI-iTools", "/api/itools"],
    "settings_crystools": ["/extensions/ComfyUI-Crystools", "/api/crystools"],
    "settings_rgthree": ["/extensions/rgthree-comfy", "/api/rgthree", "/rgthree"],
    "settings_gallery": ["/extensions/comfyui-gallery", "/api/gallery"],
    "can_access_manager": ["/extensions/comfyui-manager", "/api/manager", "/manager"],
    "can_manage_extensions": [
        "/Comfy.Extension",
        "/api/settings/Comfy/Extension",
        "/api/settings/Comfy/Extension/enable",
        "/api/settings/Comfy/Extension/Disabled",
        "/api/extensions/apply",
        "/extensions"
    ],
    "can_modify_workflows": [
        "/api/userdata/workflows:",
        "/api/userdata/workflows/save",
        "/api/userdata/workflows/export"
    ]
}


# When True, folder_paths input/output/temp use the global Comfy roots so
# admin/power can open other users' files via subfolder=<user_id>/...
_global_media_root = contextvars.ContextVar("usgromana_global_media_root", default=False)


def use_global_media_root(enabled: bool = True):
    """Context manager: serve media from global output/input/temp trees."""
    return _GlobalMediaRoot(enabled)


class _GlobalMediaRoot:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._token = None

    def __enter__(self):
        self._token = _global_media_root.set(bool(self.enabled))
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._token is not None:
            _global_media_root.reset(self._token)
        return False


class QueueLimitExceeded(Exception):
    """Raised when a user already has the max number of active jobs."""

    def __init__(self, info: dict):
        self.info = info or {}
        msg = self.info.get("error") or "Queue limit exceeded"
        super().__init__(msg)


def _usgromana_meta_from_queue_entry(entry):
    """Split Usgromana ``{user_id}`` tail from a queue heap entry."""
    if isinstance(entry, tuple) and entry and isinstance(entry[-1], dict):
        if "user_id" in entry[-1]:
            return entry[-1], entry[:-1]
    return {}, entry


def _resolve_username_for_queue(users_db: UsersDB, user_key: str | None) -> str:
    """Resolve display username from JWT user_id (UUID) or bare username."""
    if not user_key:
        return "guest"
    try:
        uid, rec = users_db.get_user(user_id=user_key)
        if rec and rec.get("username"):
            return rec["username"]
        uid, rec = users_db.get_user(username=user_key)
        if rec and rec.get("username"):
            return rec["username"]
    except Exception:
        pass
    return str(user_key)


def _inject_runner_into_extra_data(item, username: str, workflow_name: str):
    """
    Put runner identity into prompt extra_data so metadata / PNG info
    can show who ran the workflow.
    Queue item shape: (priority, prompt_id, prompt, extra_data, outputs_to_execute, ...)
    """
    if not isinstance(item, tuple) or len(item) < 4:
        return item
    extra = item[3]
    if not isinstance(extra, dict):
        extra = {}
    else:
        extra = dict(extra)
    extra["usgromana_username"] = username
    extra["usgromana_workflow"] = workflow_name
    nested = dict(extra.get("usgromana") or {})
    nested["username"] = username
    nested["workflow_name"] = workflow_name
    extra["usgromana"] = nested
    # Keep a human-readable note many UIs surface from extra_pnginfo
    png = extra.get("extra_pnginfo")
    if isinstance(png, dict):
        png = dict(png)
        png["usgromana_run_by"] = username
        png["usgromana_workflow"] = workflow_name
        extra["extra_pnginfo"] = png
    return item[:3] + (extra,) + item[4:]


def sanitize_prompt_tuple_for_api(prompt_tuple):
    """
    ComfyUI ``/api/jobs`` expects history prompt tuples with 5 elements
    (priority, prompt_id, prompt, extra_data, outputs_to_execute).
    Newer Comfy adds ``sensitive`` at index 5; strip it before persisting history.
    """
    if not isinstance(prompt_tuple, tuple):
        return prompt_tuple
    _, body = _usgromana_meta_from_queue_entry(prompt_tuple)
    if len(body) > 5:
        return body[:5]
    return body


class AccessControl:
    def __init__(self, users_db: UsersDB, server: PromptServer, groups_config_file: str):
        self.users_db = users_db
        self.server = server
        self.groups_config_file = groups_config_file

        self._current_user = contextvars.ContextVar("user_id", default=None)
        self.__current_user_id = None
        self.__get_output_directory = folder_paths.get_output_directory
        self.__get_temp_directory = folder_paths.get_temp_directory
        self.__get_input_directory = folder_paths.get_input_directory
        self.__prompt_queue = self.server.prompt_queue
        self.__prompt_queue_put = self.__prompt_queue.put
        # prompt_ids interrupted by admin/power cancel (for status + cleanup)
        self._cancelled_prompt_ids: set[str] = set()
        # task_id -> prompt_id scheduled for forced cleanup if interrupt hangs
        self._force_clear_tasks: dict = {}

    def _load_group_config(self):
        if not os.path.exists(self.groups_config_file):
            return {}
        try:
            with open(self.groups_config_file, 'r') as f:
                return json.load(f)
        except Exception:
            return {}

    def _get_user_role_and_permissions(self, request):
        token = None
        if "jwt_token" in request.cookies:
            token = request.cookies.get("jwt_token")
        if not token and "Authorization" in request.headers:
            parts = request.headers.get("Authorization", "").split(" ")
            if len(parts) == 2: token = parts[1]

        if not token: return "guest", {}, None

        try:
            import jwt
            # Decode without verification here just to get username for role lookup
            # The actual security check happens in JWTAuth middleware
            payload = jwt.decode(token, options={"verify_signature": False})
            username = payload.get("username")

            _, user_rec = self.users_db.get_user(username)
            if not user_rec: return "guest", {}, None

            groups = [g.lower() for g in user_rec.get("groups", [])]
            role = groups[0] if groups else "user"
            cfg = self._load_group_config()
            perms = cfg.get(role, {})
            return role, perms, username
        except Exception:
            return "guest", {}, None

    def create_queue_limit_middleware(self):
        """
        Enforce per-user queue caps on /prompt and attach waiting position
        to successful queue responses. Catches QueueLimitExceeded from put().
        """
        import json as _json

        @web.middleware
        async def middleware(request: web.Request, handler):
            path = request.path or ""
            is_prompt = request.method in ("POST", "PUT") and (
                path in ("/prompt", "/api/prompt") or path.rstrip("/").endswith("/prompt")
            )
            if not is_prompt:
                return await handler(request)

            uid = self.get_current_user_id()
            # Pre-check (put also checks under lock)
            try:
                status = self.get_user_queue_status(uid)
                if not status.get("can_submit") and not status.get("unlimited"):
                    return web.json_response(
                        {
                            "error": (
                                f"Queue limit: max {status.get('max_jobs')} job(s) at a time. "
                                f"You have {status.get('active')} active "
                                f"({status.get('running')} running, {status.get('pending')} waiting). "
                                f"Wait until one finishes."
                            ),
                            "code": "QUEUE_LIMIT",
                            "usgromana_queue": status,
                        },
                        status=429,
                    )
            except Exception as e:
                print(f"[Usgromana] queue pre-check error: {e}")

            try:
                response = await handler(request)
            except QueueLimitExceeded as e:
                info = dict(e.info or {})
                try:
                    info["usgromana_queue"] = self.get_user_queue_status(uid)
                except Exception:
                    pass
                return web.json_response(info, status=429)
            except Exception as e:
                # Some Comfy versions wrap exceptions
                cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
                if isinstance(e, QueueLimitExceeded):
                    return web.json_response(e.info or {"error": str(e)}, status=429)
                if isinstance(cause, QueueLimitExceeded):
                    return web.json_response(cause.info or {"error": str(cause)}, status=429)
                raise

            # Enrich successful prompt response with waiting number
            try:
                if response.status == 200 and hasattr(response, "body") and response.body:
                    ctype = (response.content_type or "").lower()
                    if "json" in ctype:
                        payload = _json.loads(response.body)
                        if isinstance(payload, dict) and "error" not in payload:
                            qstatus = self.get_user_queue_status(uid)
                            payload["usgromana_queue"] = qstatus
                            # Friendly top-level fields for clients
                            payload["waiting_number"] = qstatus.get("waiting_number")
                            payload["jobs_ahead"] = qstatus.get("jobs_ahead")
                            payload["queue_active"] = qstatus.get("active")
                            payload["queue_max"] = qstatus.get("max_jobs")
                            return web.json_response(payload, status=200)
            except Exception as e:
                print(f"[Usgromana] queue response enrich failed: {e}")

            return response

        return middleware

    def create_usgromana_middleware(self):
        @web.middleware
        async def middleware(request: web.Request, handler):
            path = request.path
            
            # 1. Public Whitelist
            if (path.startswith(("/login", "/register", "/logout", "/usgromana", "/usgromana-gallery", "/static", "/favicon", "/ws", "/assets")) or path == "/"):
                return await handler(request)
            
            # 2. Core Extensions
            if path.startswith(("/extensions/core", "/extensions/ComfyUI-Usgromana", "/extensions/Usgromana")):
                return await handler(request)

            # 3. Resolve User
            role, perms, username = self._get_user_role_and_permissions(request)

            # 4. Check Permissions
            is_queue = path.startswith(("/prompt", "/api/prompt", "/api/queue", "/queue"))
            is_upload = path.startswith(("/upload", "/api/upload"))
            is_userdata_workflow = path.startswith(("/api/userdata/workflows", "/api/userdata/workflows:"))

            if is_queue and perms.get("can_run") is False:
                return web.json_response({"error": "Krish: Execution Denied"}, status=403)

            if is_upload and perms.get("can_upload") is False:
                return web.json_response({"error": "Krish: Upload Denied"}, status=403)

            if is_userdata_workflow and request.method in ("POST", "PUT", "DELETE", "PATCH"):
                can_modify = perms.get("can_modify_workflows")
                if can_modify is None: can_modify = (role != "guest")
                if role == "admin": can_modify = True

                if not can_modify:
                    return web.json_response({"error": "Krish: Workflow Denied", "code": "WORKFLOW_DENIED", "role": role}, status=403)

            for perm_key, blocked_paths in EXTENSION_BLOCK_MAP.items():
                allow = perms.get(perm_key)
                if allow is None: allow = (role != "guest")
                if role == "admin": allow = True
                if allow is False:
                    for blocked_prefix in blocked_paths:
                        if path.lower().startswith(blocked_prefix.lower()):
                            return web.Response(status=403, text="Krish: Access Denied")

            if not is_queue and not is_upload and path.startswith("/api/"):
                if perms.get("can_access_api") is False:
                    return web.json_response({"error": "Krish: API Denied"}, status=403)

            return await handler(request)
        return middleware

    # --- Folder & User Context Methods ---

    def set_current_user_id(self, user_id: str, set_fallback=False):
        self._current_user.set(user_id)
        if set_fallback: self.__current_user_id = user_id
    
    def get_current_user_id(self):
        return self._current_user.get() or self.__current_user_id

    def user_can_view_all(self, user_key: str | None = None) -> bool:
        """
        True for admin / power accounts.
        They may see every user's queue, history, and outputs.
        """
        key = user_key if user_key is not None else self.get_current_user_id()
        if not key:
            return False
        try:
            uid, rec = self.users_db.get_user(user_id=key)
            if not rec:
                uid, rec = self.users_db.get_user(username=key)
            if not rec:
                return False
            if rec.get("admin"):
                return True
            groups = [str(g).lower() for g in (rec.get("groups") or [])]
            return "admin" in groups or "power" in groups
        except Exception:
            return False

    def current_user_can_view_all(self) -> bool:
        return self.user_can_view_all(self.get_current_user_id())

    def _resolved_directory_user_id(self) -> str | None:
        """User id for per-user folder roots, or None when no request context (e.g. startup asset scan)."""
        uid = self.get_current_user_id()
        return uid if uid else None

    def storage_folder_name(self, user_key: str | None = None) -> str | None:
        """
        Folder name under output/input/temp for a user.
        Prefer stable **username** (e.g. output/alice/) so paths are readable.
        Falls back to sanitized UUID / key if username unknown.
        """
        import re

        key = user_key if user_key is not None else self._resolved_directory_user_id()
        if not key:
            return None
        name = None
        try:
            uid, rec = self.users_db.get_user(user_id=str(key))
            if not rec:
                uid, rec = self.users_db.get_user(username=str(key))
            if rec and rec.get("username"):
                name = rec.get("username")
        except Exception:
            name = None
        if not name:
            name = str(key)
        # Safe filesystem segment (Windows-friendly)
        safe = re.sub(r"[^A-Za-z0-9_\-\.]", "_", str(name)).strip(" ._")
        return safe or "user"

    def _user_media_dir(self, base: str, user_key: str | None = None) -> str:
        folder = self.storage_folder_name(user_key)
        if not folder:
            return base
        path = os.path.join(base, folder)
        os.makedirs(path, exist_ok=True)
        return path

    def get_user_output_directory(self):
        base = self.__get_output_directory()
        # Admin/power HTTP media reads use the global tree when flagged.
        if _global_media_root.get():
            return base
        return self._user_media_dir(base)

    def get_user_temp_directory(self):
        base = self.__get_temp_directory()
        if _global_media_root.get():
            return base
        return self._user_media_dir(base)

    def get_user_input_directory(self):
        base = self.__get_input_directory()
        if _global_media_root.get():
            return base
        return self._user_media_dir(base)

    def get_user_storage_prefixes(self, user_id: str | None = None) -> list[str]:
        """Absolute paths for a user's isolated output/input/temp folders."""
        key = user_id or self._resolved_directory_user_id()
        folder = self.storage_folder_name(key)
        if not folder:
            return []
        prefixes = []
        # Also include legacy UUID folder if different from username
        aliases = {folder}
        if key and str(key) != folder:
            aliases.add(str(key))
        for base in (
            self.__get_output_directory(),
            self.__get_input_directory(),
            self.__get_temp_directory(),
        ):
            for name in aliases:
                path = os.path.join(base, name)
                os.makedirs(path, exist_ok=True)
                prefixes.append(os.path.abspath(path))
        return prefixes

    @staticmethod
    def _sanitize_filename_prefix(value: str, folder_name: str | None) -> str:
        """
        Force SaveImage (and similar) prefixes to stay relative under the
        per-user output root. Strips absolute paths / drive letters / '..'.
        """
        clean = (value or "ComfyUI").replace("\\", "/")
        # Drop Windows drive / UNC / absolute roots
        if ":" in clean:
            clean = clean.split(":")[-1]
        clean = clean.lstrip("/")
        parts = [p for p in clean.split("/") if p and p not in (".", "..")]
        if not parts:
            parts = ["ComfyUI"]
        # Remove accidental nesting of username or output/temp roots
        skip = {"output", "outputs", "input", "temp", "ComfyUI"}
        if folder_name:
            skip.add(folder_name.lower())
        while parts and parts[0].lower() in {s.lower() for s in skip if s}:
            # keep "ComfyUI" if it's the only remaining filename stem
            if len(parts) == 1 and parts[0].lower() == "comfyui":
                break
            if parts[0].lower() == "comfyui" and len(parts) > 1:
                break
            if folder_name and parts[0].lower() == folder_name.lower():
                parts = parts[1:]
                continue
            if parts[0].lower() in ("output", "outputs", "input", "temp"):
                parts = parts[1:]
                continue
            break
        return "/".join(parts) if parts else "ComfyUI"

    def add_user_specific_folder_paths(self, json_data):
        """
        On each prompt: force all save paths into the current user's media root.
        Workflow-specified absolute/other locations are rewritten.
        """
        folder = self.storage_folder_name()
        if not folder:
            return json_data
        if isinstance(json_data, dict):
            for k, v in list(json_data.items()):
                if k in ("filename_prefix", "output_path", "save_path") and isinstance(v, str):
                    json_data[k] = self._sanitize_filename_prefix(v, folder)
                elif k == "subfolder" and isinstance(v, str):
                    # Keep relative subfolder only; strip user/output escapes
                    json_data[k] = self._sanitize_filename_prefix(v, folder)
                    if json_data[k] == "ComfyUI":
                        json_data[k] = ""
                else:
                    self.add_user_specific_folder_paths(v)
        elif isinstance(json_data, list):
            for item in json_data:
                self.add_user_specific_folder_paths(item)
        return json_data

    def patch_folder_paths(self):
        # Match ComfyUI Assets view: each user sees their own input/output/temp roots.
        folder_paths.get_output_directory = self.get_user_output_directory
        folder_paths.get_temp_directory = self.get_user_temp_directory
        folder_paths.get_input_directory = self.get_user_input_directory
        self.server.add_on_prompt_handler(self.add_user_specific_folder_paths)

    # --- MISSING METHOD RESTORED HERE ---
    def create_folder_access_control_middleware(self):
        folder_paths_check = (
            self.__get_output_directory(),
            self.__get_temp_directory(),
            self.__get_input_directory(),
        )

        @web.middleware
        async def middleware(request: web.Request, handler):
            if not request.path.startswith(folder_paths_check):
                return await handler(request)
            # Future expansion: Check permissions for specific file access here
            return await handler(request)

        return middleware

    # --- Queue Patching ---

    def patch_prompt_queue(self):
        self.__prompt_queue.put = self.user_queue_put
        self.__prompt_queue.get = self.user_queue_get
        self.__prompt_queue.task_done = self.user_queue_task_done
        self.__prompt_queue.get_current_queue = self.user_queue_get_current_queue
        self.__prompt_queue.wipe_queue = self.user_queue_wipe_queue
        self.__prompt_queue.delete_queue_item = self.user_queue_delete_queue_item
        self.__prompt_queue.get_history = self.user_queue_get_history
        self.__prompt_queue.wipe_history = self.user_queue_wipe_history

    def _user_role_for_limit(self, user_key: str | None) -> str:
        if not user_key:
            return "guest"
        try:
            _, rec = self.users_db.get_user(user_id=user_key)
            if not rec:
                _, rec = self.users_db.get_user(username=user_key)
            if not rec:
                return "guest"
            if rec.get("admin"):
                return "admin"
            groups = [str(g).lower() for g in (rec.get("groups") or [])]
            for role in ("admin", "power", "user", "guest"):
                if role in groups:
                    return role
        except Exception:
            pass
        return "user"

    def _queue_limit_for_user(self, user_key: str | None) -> int:
        """Max concurrent jobs (pending+running). 0 = unlimited."""
        try:
            from ..constants import MAX_QUEUE_JOBS_PER_USER, QUEUE_LIMIT_EXEMPT_ROLES
        except Exception:
            return 2
        role = self._user_role_for_limit(user_key)
        if role in QUEUE_LIMIT_EXEMPT_ROLES:
            return 0
        return max(0, int(MAX_QUEUE_JOBS_PER_USER or 0))

    def _count_user_jobs_unlocked(self, user_id: str | None) -> dict:
        """Count pending/running jobs for a user. Caller must hold queue mutex."""
        pending = 0
        running = 0
        if not user_id:
            return {"pending": 0, "running": 0, "active": 0}
        for item in self.__prompt_queue.currently_running.values():
            meta, _ = _usgromana_meta_from_queue_entry(item)
            if meta.get("user_id") == user_id:
                running += 1
        for item in self.__prompt_queue.queue:
            meta, _ = _usgromana_meta_from_queue_entry(item)
            if meta.get("user_id") == user_id:
                pending += 1
        return {
            "pending": pending,
            "running": running,
            "active": pending + running,
        }

    def count_user_jobs(self, user_id: str | None = None) -> dict:
        uid = user_id if user_id is not None else self.get_current_user_id()
        with self.__prompt_queue.mutex:
            return self._count_user_jobs_unlocked(uid)

    def get_user_queue_status(self, user_id: str | None = None) -> dict:
        """
        Status for the user: active job counts, limit, and wait positions
        of their pending jobs in the global queue (1 = next to run).
        """
        uid = user_id if user_id is not None else self.get_current_user_id()
        username = _resolve_username_for_queue(self.users_db, uid)
        max_jobs = self._queue_limit_for_user(uid)
        with self.__prompt_queue.mutex:
            counts = self._count_user_jobs_unlocked(uid)
            # Global ordered list: running first (as "executing"), then pending heap order
            global_slots = []
            for item in self.__prompt_queue.currently_running.values():
                meta, body = _usgromana_meta_from_queue_entry(item)
                prompt_id = body[1] if isinstance(body, tuple) and len(body) > 1 else None
                global_slots.append(
                    {
                        "status": "running",
                        "user_id": meta.get("user_id"),
                        "username": meta.get("username")
                        or _resolve_username_for_queue(self.users_db, meta.get("user_id")),
                        "prompt_id": prompt_id,
                        "workflow_name": meta.get("workflow_name") or "Unnamed workflow",
                    }
                )
            # heap order = execution order
            pending_sorted = sorted(
                list(self.__prompt_queue.queue),
                key=lambda e: e[0] if isinstance(e, tuple) and e else 0,
            )
            for item in pending_sorted:
                meta, body = _usgromana_meta_from_queue_entry(item)
                prompt_id = body[1] if isinstance(body, tuple) and len(body) > 1 else None
                global_slots.append(
                    {
                        "status": "queued",
                        "user_id": meta.get("user_id"),
                        "username": meta.get("username")
                        or _resolve_username_for_queue(self.users_db, meta.get("user_id")),
                        "prompt_id": prompt_id,
                        "workflow_name": meta.get("workflow_name") or "Unnamed workflow",
                    }
                )

        my_positions = []
        for idx, slot in enumerate(global_slots, start=1):
            if slot.get("user_id") == uid:
                my_positions.append(
                    {
                        "waiting_number": idx,
                        "status": slot["status"],
                        "prompt_id": slot.get("prompt_id"),
                        "workflow_name": slot.get("workflow_name"),
                        "jobs_ahead": idx - 1,
                    }
                )

        # Primary position = first of user's jobs in global order (or next free slot)
        if my_positions:
            primary = my_positions[0]
            waiting_number = primary["waiting_number"]
            jobs_ahead = primary["jobs_ahead"]
        else:
            waiting_number = len(global_slots) + 1
            jobs_ahead = len(global_slots)

        return {
            "username": username,
            "user_id": uid,
            "pending": counts["pending"],
            "running": counts["running"],
            "active": counts["active"],
            "max_jobs": max_jobs,
            "unlimited": max_jobs == 0,
            "can_submit": max_jobs == 0 or counts["active"] < max_jobs,
            "waiting_number": waiting_number,
            "jobs_ahead": jobs_ahead,
            "my_jobs": my_positions,
            "server_queue_length": len(global_slots),
        }

    def user_queue_put(self, item):
        current_user_id = self.get_current_user_id()
        # get_user expects user_id= for UUIDs (JWT sets UUID, not username)
        _, user_rec = self.users_db.get_user(user_id=current_user_id or "")
        if not user_rec and current_user_id:
            _, user_rec = self.users_db.get_user(username=current_user_id)

        if user_rec:
            if os.path.exists(self.groups_config_file):
                try:
                    with open(self.groups_config_file, 'r') as f:
                        cfg = json.load(f)
                    groups = user_rec.get("groups", ["user"])
                    role = groups[0] if groups else "user"
                    perms = cfg.get(role, {})
                    if perms.get("can_run") is False:
                        print(f"[AccessControl] Blocked execution for {current_user_id}")
                        return
                except Exception:
                    pass

        username = _resolve_username_for_queue(self.users_db, current_user_id)
        from .workflow_run_log import WorkflowRunLog, get_run_log

        parsed = WorkflowRunLog.extract_prompt_meta(item)
        workflow_name = parsed.get("workflow_name") or "Unnamed workflow"
        prompt_id = parsed.get("prompt_id")
        node_count = parsed.get("node_count") or 0

        # Tag extra_data so generated media / history can show the runner name
        if isinstance(item, tuple):
            item = _inject_runner_into_extra_data(item, username, workflow_name)
            new_item = (
                *item,
                {
                    "user_id": current_user_id,
                    "username": username,
                    "storage_folder": self.storage_folder_name(username or current_user_id),
                    "workflow_name": workflow_name,
                },
            )
        else:
            new_item = (
                item,
                {
                    "user_id": current_user_id,
                    "username": username,
                    "storage_folder": self.storage_folder_name(username or current_user_id),
                    "workflow_name": workflow_name,
                },
            )

        max_jobs = self._queue_limit_for_user(current_user_id)
        # Atomic check + enqueue under the same lock as Comfy's queue
        with self.__prompt_queue.mutex:
            counts = self._count_user_jobs_unlocked(current_user_id)
            if max_jobs > 0 and counts["active"] >= max_jobs:
                info = {
                    "error": (
                        f"Queue limit: you may only have {max_jobs} job(s) "
                        f"running/waiting at a time. "
                        f"You currently have {counts['active']} "
                        f"({counts['running']} running, {counts['pending']} waiting). "
                        f"Wait until one finishes before submitting another."
                    ),
                    "code": "QUEUE_LIMIT",
                    "active": counts["active"],
                    "running": counts["running"],
                    "pending": counts["pending"],
                    "max_jobs": max_jobs,
                    "username": username,
                }
                print(
                    f"[Usgromana] Queue LIMIT user={username!r} "
                    f"active={counts['active']}/{max_jobs}"
                )
                raise QueueLimitExceeded(info)

            # Same body as PromptQueue.put (avoid nested mutex deadlock).
            # not_empty is usually a Condition on the same mutex — notify while held.
            heapq.heappush(self.__prompt_queue.queue, new_item)
            try:
                self.server.queue_updated()
            except Exception:
                pass
            try:
                ne = self.__prompt_queue.not_empty
                if hasattr(ne, "notify"):
                    ne.notify()
                elif hasattr(ne, "notify_all"):
                    ne.notify_all()
            except RuntimeError:
                # notify() called without owning the Condition lock on some builds
                try:
                    with self.__prompt_queue.not_empty:
                        self.__prompt_queue.not_empty.notify()
                except Exception:
                    pass
            except Exception:
                pass

        try:
            get_run_log().log_queued(
                prompt_id=prompt_id,
                user_id=current_user_id,
                username=username,
                workflow_name=workflow_name,
                node_count=node_count,
                status="queued",
            )
            status = self.get_user_queue_status(current_user_id)
            print(
                f"[Usgromana] Queue: user={username!r} workflow={workflow_name!r} "
                f"prompt_id={prompt_id!r} waiting_number={status.get('waiting_number')}"
            )
        except Exception as e:
            print(f"[Usgromana] workflow run log (queue) failed: {e}")

    def user_queue_get(self, timeout=None):
        with self.__prompt_queue.not_empty:
            while not self.__prompt_queue.queue:
                self.__prompt_queue.not_empty.wait(timeout=timeout)
                if timeout and not self.__prompt_queue.queue:
                    return None
            entry = heapq.heappop(self.__prompt_queue.queue)
            task_id = self.__prompt_queue.task_counter
            self.__prompt_queue.currently_running[task_id] = entry
            self.__prompt_queue.task_counter += 1
            self.server.queue_updated()

            # Mark run as actively executing
            try:
                meta, body = _usgromana_meta_from_queue_entry(entry)
                # Critical: bind worker output dirs to the job owner, not the
                # last HTTP request (admin polling must not steal another user's saves).
                owner_id = meta.get("user_id")
                uname = meta.get("username") or _resolve_username_for_queue(
                    self.users_db, meta.get("user_id")
                )
                # Prefer username as storage key so output lands in output/<username>/
                storage_key = uname if uname and uname != "guest" else owner_id
                if storage_key:
                    self.set_current_user_id(storage_key, set_fallback=True)
                # Ensure folder exists before nodes save
                try:
                    out_dir = self.get_user_output_directory()
                    os.makedirs(out_dir, exist_ok=True)
                except Exception:
                    pass
                prompt_id = body[1] if isinstance(body, tuple) and len(body) > 1 else None
                if prompt_id:
                    from .workflow_run_log import get_run_log

                    get_run_log().update_status(str(prompt_id), "running")
                wf = meta.get("workflow_name") or "Unnamed workflow"
                print(
                    f"[Usgromana] Running: user={uname!r} out={self.storage_folder_name(storage_key)!r} "
                    f"workflow={wf!r} prompt_id={prompt_id!r}"
                )
            except Exception as e:
                print(f"[Usgromana] workflow run log (start) failed: {e}")

            return (entry, task_id)

    def user_queue_task_done(self, item_id, history_result, **kwargs):
        process_item = kwargs.get("process_item")
        status = kwargs.get("status")
        with self.__prompt_queue.mutex:
            # Safe pop: admin cancel must not crash the worker if entry was already cleaned up
            item = self.__prompt_queue.currently_running.pop(item_id, None)
            if item is None:
                # Still notify UI so queue is not stuck visually
                try:
                    self.server.queue_updated()
                except Exception:
                    pass
                return
            while len(self.__prompt_queue.history) > MAXIMUM_HISTORY_SIZE:
                self.__prompt_queue.history.pop(next(iter(self.__prompt_queue.history)))

            meta, prompt_body = _usgromana_meta_from_queue_entry(item)
            if process_item is not None:
                prompt_stored = process_item(prompt_body)
            else:
                prompt_stored = sanitize_prompt_tuple_for_api(prompt_body)

            if status is not None and hasattr(status, "_asdict"):
                status_dict = copy.deepcopy(status._asdict())
            else:
                status_dict = {
                    "completed": kwargs.get("completed"),
                    "messages": kwargs.get("messages"),
                }

            prompt_id = prompt_stored[1]
            username = meta.get("username") or _resolve_username_for_queue(
                self.users_db, meta.get("user_id")
            )
            workflow_name = meta.get("workflow_name") or "Unnamed workflow"
            was_cancelled = False
            try:
                if str(prompt_id) in self._cancelled_prompt_ids:
                    was_cancelled = True
                    self._cancelled_prompt_ids.discard(str(prompt_id))
            except Exception:
                pass
            if was_cancelled and isinstance(status_dict, dict):
                status_dict = dict(status_dict)
                status_dict["status_str"] = "cancelled"
                status_dict["completed"] = False
            self.__prompt_queue.history[prompt_id] = {
                "prompt": prompt_stored,
                "outputs": {},
                "status": status_dict,
                "user_id": meta.get("user_id"),
                "username": username,
                "workflow_name": workflow_name,
            }
            # Finalize run log status
            try:
                from .workflow_run_log import get_run_log

                completed_flag = None
                if isinstance(status_dict, dict):
                    completed_flag = status_dict.get("completed")
                elif hasattr(status, "completed"):
                    completed_flag = getattr(status, "completed", None)
                if was_cancelled:
                    final_status = "cancelled"
                elif completed_flag is False:
                    final_status = "error"
                elif completed_flag is True:
                    final_status = "completed"
                else:
                    final_status = "completed"
                get_run_log().update_status(
                    str(prompt_id) if prompt_id else None,
                    final_status,
                    finished=True,
                )
            except Exception as e:
                print(f"[Usgromana] workflow run log (done) failed: {e}")

            if history_result:
                self.__prompt_queue.history[prompt_id].update(history_result)
                prompt_user = meta.get("user_id")
                if prompt_user:
                    try:
                        self.set_current_user_id(prompt_user, set_fallback=True)
                        from .sfw_intercept.nsfw_guard import (
                            tag_output_images_from_history,
                        )

                        tag_output_images_from_history(history_result)
                        from .comfy_user_bridge import register_outputs_from_history

                        register_outputs_from_history(history_result, prompt_user)
                    except Exception as e:
                        print(f"[Usgromana] post-prompt hooks: {e}")
            self.server.queue_updated()

    def _history_entry_for_viewer(self, entry: dict, *, can_view_all: bool) -> dict:
        """
        Copy a history row for the API.

        Image paths are left as Comfy saved them (relative to that job owner's
        output/temp root). Do NOT rewrite subfolders here — that caused
        "Image failed to load" when combined with per-user folder chroots.
        Privileged /view resolution searches all user folders instead.
        """
        out = copy.deepcopy(entry)
        if "prompt" in out:
            out["prompt"] = sanitize_prompt_tuple_for_api(out["prompt"])
        # Expose runner identity for UI without altering media paths
        if can_view_all and out.get("username"):
            out.setdefault("usgromana_username", out.get("username"))
        return out

    def user_queue_get_current_queue(self):
        def unwrap(entry):
            _, body = _usgromana_meta_from_queue_entry(entry)
            return sanitize_prompt_tuple_for_api(body)

        current_user = self.get_current_user_id()
        can_view_all = self.current_user_can_view_all()
        with self.__prompt_queue.mutex:
            running = []
            pending = []
            for item in self.__prompt_queue.currently_running.values():
                meta = item[-1] if isinstance(item[-1], dict) else None
                if not can_view_all:
                    if not meta or meta.get("user_id") != current_user:
                        continue
                running.append(unwrap(item))
            for item in self.__prompt_queue.queue:
                meta = item[-1] if isinstance(item[-1], dict) else None
                if not can_view_all:
                    if not meta or meta.get("user_id") != current_user:
                        continue
                pending.append(unwrap(item))
            return (running, copy.deepcopy(pending))

    def get_active_runs_snapshot(self) -> list[dict]:
        """Currently running + pending jobs with username / workflow (all users)."""
        from .workflow_run_log import WorkflowRunLog

        out: list[dict] = []
        try:
            with self.__prompt_queue.mutex:
                for item in self.__prompt_queue.currently_running.values():
                    meta, body = _usgromana_meta_from_queue_entry(item)
                    parsed = WorkflowRunLog.extract_prompt_meta(body)
                    uname = meta.get("username") or _resolve_username_for_queue(
                        self.users_db, meta.get("user_id")
                    )
                    out.append(
                        {
                            "status": "running",
                            "prompt_id": parsed.get("prompt_id"),
                            "user_id": meta.get("user_id"),
                            "username": uname,
                            "workflow_name": meta.get("workflow_name")
                            or parsed.get("workflow_name")
                            or "Unnamed workflow",
                            "node_count": parsed.get("node_count") or 0,
                        }
                    )
                for item in self.__prompt_queue.queue:
                    meta, body = _usgromana_meta_from_queue_entry(item)
                    parsed = WorkflowRunLog.extract_prompt_meta(body)
                    uname = meta.get("username") or _resolve_username_for_queue(
                        self.users_db, meta.get("user_id")
                    )
                    out.append(
                        {
                            "status": "queued",
                            "prompt_id": parsed.get("prompt_id"),
                            "user_id": meta.get("user_id"),
                            "username": uname,
                            "workflow_name": meta.get("workflow_name")
                            or parsed.get("workflow_name")
                            or "Unnamed workflow",
                            "node_count": parsed.get("node_count") or 0,
                        }
                    )
        except Exception as e:
            print(f"[Usgromana] get_active_runs_snapshot failed: {e}")
        return out

    def user_queue_wipe_queue(self):
        """
        Clear only the current user's pending items.

        IMPORTANT: Do not wipe the global queue for admin/power here — ComfyUI's
        "Clear Queue" would otherwise delete every user's jobs.
        """
        with self.__prompt_queue.mutex:
            current_user = self.get_current_user_id()
            self.__prompt_queue.queue = [
                i
                for i in self.__prompt_queue.queue
                if not (
                    isinstance(i[-1], dict) and i[-1].get("user_id") == current_user
                )
            ]
            self.server.queue_updated()

    def _interrupt_comfy_execution(self) -> None:
        """Ask ComfyUI to stop the current running prompt (safe for the worker thread)."""
        # Prefer official interrupt flags used by /interrupt
        try:
            import nodes

            if hasattr(nodes, "interrupt_processing"):
                nodes.interrupt_processing(True)
        except Exception as e:
            print(f"[Usgromana] nodes.interrupt_processing: {e}")
        try:
            import comfy.model_management as mm

            if hasattr(mm, "interrupt_current_processing"):
                mm.interrupt_current_processing(True)
            elif hasattr(mm, "interrupt_processing"):
                mm.interrupt_processing(True)
        except Exception as e:
            print(f"[Usgromana] model_management interrupt: {e}")
        try:
            interrupt = getattr(self.server, "interrupt_processing", None) or getattr(
                self.server, "interrupt_current", None
            )
            if callable(interrupt):
                interrupt()
        except Exception as e:
            print(f"[Usgromana] server interrupt: {e}")

    def cancel_job_by_prompt_id(self, prompt_id: str, *, actor_can_view_all: bool = False) -> dict:
        """
        Cancel a pending or running job by prompt_id.

        Pending: remove from heap immediately.
        Running: interrupt Comfy execution and leave currently_running for the
        worker to finish via task_done (do NOT pop it here — that bricks later runs).
        """
        if not prompt_id:
            return {"ok": False, "error": "Missing prompt_id"}
        pid = str(prompt_id)
        current_user = self.get_current_user_id()
        found = None
        need_interrupt = False
        force_task_id = None

        with self.__prompt_queue.mutex:
            # 1) Pending queue — safe to remove immediately
            for i, item in enumerate(list(self.__prompt_queue.queue)):
                meta, body = _usgromana_meta_from_queue_entry(item)
                body_pid = body[1] if isinstance(body, tuple) and len(body) > 1 else None
                if str(body_pid) != pid:
                    continue
                owner = meta.get("user_id")
                if not actor_can_view_all and owner != current_user:
                    return {
                        "ok": False,
                        "error": "Not allowed to cancel this job",
                        "code": "FORBIDDEN",
                    }
                self.__prompt_queue.queue.pop(i)
                heapq.heapify(self.__prompt_queue.queue)
                found = {
                    "ok": True,
                    "cancelled": "pending",
                    "prompt_id": pid,
                    "user_id": owner,
                    "username": meta.get("username")
                    or _resolve_username_for_queue(self.users_db, owner),
                    "workflow_name": meta.get("workflow_name") or "Unnamed workflow",
                }
                break

            # 2) Running — only interrupt; worker must call task_done itself
            if not found:
                for task_id, item in list(self.__prompt_queue.currently_running.items()):
                    meta, body = _usgromana_meta_from_queue_entry(item)
                    body_pid = body[1] if isinstance(body, tuple) and len(body) > 1 else None
                    if str(body_pid) != pid:
                        continue
                    owner = meta.get("user_id")
                    if not actor_can_view_all and owner != current_user:
                        return {
                            "ok": False,
                            "error": "Not allowed to cancel this job",
                            "code": "FORBIDDEN",
                        }
                    self._cancelled_prompt_ids.add(pid)
                    need_interrupt = True
                    force_task_id = task_id
                    found = {
                        "ok": True,
                        "cancelled": "running",
                        "prompt_id": pid,
                        "user_id": owner,
                        "username": meta.get("username")
                        or _resolve_username_for_queue(self.users_db, owner),
                        "workflow_name": meta.get("workflow_name") or "Unnamed workflow",
                        "note": "Interrupt sent; slot frees when the worker stops (or after timeout)",
                    }
                    break

            if found and found.get("cancelled") == "pending":
                self.server.queue_updated()

        if not found:
            return {"ok": False, "error": "Job not found in queue", "code": "NOT_FOUND"}

        if need_interrupt:
            self._interrupt_comfy_execution()
            try:
                self.server.queue_updated()
            except Exception:
                pass
            # If the worker never finishes task_done, free the slot so the user can run again
            if force_task_id is not None:
                self._schedule_force_clear_running(pid, force_task_id)

        try:
            from .workflow_run_log import get_run_log

            # Pending is fully gone now; running finishes when task_done or force-clear runs
            get_run_log().update_status(
                pid,
                "cancelled",
                finished=(found.get("cancelled") == "pending"),
            )
        except Exception:
            pass

        return found

    def _schedule_force_clear_running(self, prompt_id: str, task_id, delay_sec: float = 25.0):
        """
        If interrupt does not complete, remove a stuck currently_running entry so
        queue limits and later jobs keep working.
        """
        import threading

        pid = str(prompt_id)

        def _worker():
            import time as _time

            _time.sleep(delay_sec)
            try:
                with self.__prompt_queue.mutex:
                    item = self.__prompt_queue.currently_running.get(task_id)
                    if item is None:
                        return
                    meta, body = _usgromana_meta_from_queue_entry(item)
                    body_pid = body[1] if isinstance(body, tuple) and len(body) > 1 else None
                    if str(body_pid) != pid:
                        return
                    # Still stuck after interrupt — force free
                    self.__prompt_queue.currently_running.pop(task_id, None)
                    self._cancelled_prompt_ids.discard(pid)
                    try:
                        self.server.queue_updated()
                    except Exception:
                        pass
                try:
                    from .workflow_run_log import get_run_log

                    get_run_log().update_status(pid, "cancelled", finished=True)
                except Exception:
                    pass
                print(
                    f"[Usgromana] Force-cleared stuck cancelled job prompt_id={pid!r} "
                    f"task_id={task_id}"
                )
            except Exception as e:
                print(f"[Usgromana] force-clear failed: {e}")

        t = threading.Thread(target=_worker, name=f"usgromana-force-clear-{pid[:8]}", daemon=True)
        t.start()

    def user_queue_delete_queue_item(self, func):
        def unwrap(entry):
            _, body = _usgromana_meta_from_queue_entry(entry)
            return sanitize_prompt_tuple_for_api(body)

        current_user = self.get_current_user_id()
        can_view_all = self.current_user_can_view_all()
        with self.__prompt_queue.mutex:
            for i, item in enumerate(self.__prompt_queue.queue):
                meta = item[-1] if isinstance(item[-1], dict) else None
                if not meta:
                    continue
                if not can_view_all and meta.get("user_id") != current_user:
                    continue
                if func(unwrap(item)):
                    self.__prompt_queue.queue.pop(i)
                    heapq.heapify(self.__prompt_queue.queue)
                    self.server.queue_updated()
                    return True
        return False

    def user_queue_get_history(self, prompt_id=None, max_items=None, offset=-1):
        with self.__prompt_queue.mutex:
            user = self.get_current_user_id()
            can_view_all = self.current_user_can_view_all()
            if can_view_all:
                filtered = dict(self.__prompt_queue.history)
            else:
                filtered = {
                    k: v
                    for k, v in self.__prompt_queue.history.items()
                    if v.get("user_id") == user
                }
            if prompt_id:
                if prompt_id not in filtered:
                    return {}
                return {
                    prompt_id: self._history_entry_for_viewer(
                        filtered[prompt_id], can_view_all=can_view_all
                    )
                }
            keys = list(filtered.keys())
            if offset < 0:
                offset = max(0, len(keys) - max_items) if max_items else 0
            result = {}
            for k in keys[offset:]:
                result[k] = self._history_entry_for_viewer(
                    filtered[k], can_view_all=can_view_all
                )
                if max_items and len(result) >= max_items:
                    break
            return result

    def user_queue_wipe_history(self):
        with self.__prompt_queue.mutex:
            u = self.get_current_user_id()
            if self.current_user_can_view_all():
                self.__prompt_queue.history = {}
            else:
                self.__prompt_queue.history = {
                    k: v
                    for k, v in self.__prompt_queue.history.items()
                    if v.get("user_id") != u
                }
