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
                return web.json_response({"error": "Usgromana: Execution Denied"}, status=403)

            if is_upload and perms.get("can_upload") is False:
                return web.json_response({"error": "Usgromana: Upload Denied"}, status=403)

            if is_userdata_workflow and request.method in ("POST", "PUT", "DELETE", "PATCH"):
                can_modify = perms.get("can_modify_workflows")
                if can_modify is None: can_modify = (role != "guest")
                if role == "admin": can_modify = True

                if not can_modify:
                    return web.json_response({"error": "Usgromana: Workflow Denied", "code": "WORKFLOW_DENIED", "role": role}, status=403)

            for perm_key, blocked_paths in EXTENSION_BLOCK_MAP.items():
                allow = perms.get(perm_key)
                if allow is None: allow = (role != "guest")
                if role == "admin": allow = True
                if allow is False:
                    for blocked_prefix in blocked_paths:
                        if path.lower().startswith(blocked_prefix.lower()):
                            return web.Response(status=403, text="Usgromana: Access Denied")

            if not is_queue and not is_upload and path.startswith("/api/"):
                if perms.get("can_access_api") is False:
                    return web.json_response({"error": "Usgromana: API Denied"}, status=403)

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

    def get_user_output_directory(self):
        base = self.__get_output_directory()
        # Admin/power HTTP media reads use the global tree (subfolder holds user_id).
        if _global_media_root.get():
            return base
        uid = self._resolved_directory_user_id()
        if not uid:
            return base
        path = os.path.join(base, uid)
        os.makedirs(path, exist_ok=True)
        return path

    def get_user_temp_directory(self):
        base = self.__get_temp_directory()
        if _global_media_root.get():
            return base
        uid = self._resolved_directory_user_id()
        if not uid:
            return base
        path = os.path.join(base, uid)
        os.makedirs(path, exist_ok=True)
        return path

    def get_user_input_directory(self):
        base = self.__get_input_directory()
        if _global_media_root.get():
            return base
        uid = self._resolved_directory_user_id()
        if not uid:
            return base
        path = os.path.join(base, uid)
        os.makedirs(path, exist_ok=True)
        return path

    def get_user_storage_prefixes(self, user_id: str | None = None) -> list[str]:
        """Absolute paths for a user's isolated output/input/temp folders."""
        uid = user_id or self._resolved_directory_user_id()
        if not uid:
            return []
        prefixes = []
        for base in (
            self.__get_output_directory(),
            self.__get_input_directory(),
            self.__get_temp_directory(),
        ):
            path = os.path.join(base, uid)
            os.makedirs(path, exist_ok=True)
            prefixes.append(os.path.abspath(path))
        return prefixes

    def add_user_specific_folder_paths(self, json_data):
        user_id = self._resolved_directory_user_id()
        if not user_id:
            return json_data
        if isinstance(json_data, dict):
            for k, v in json_data.items():
                if k == "filename_prefix" and isinstance(v, str):
                    # input/output/temp roots are already per-user; do not nest user_id again.
                    clean = v.replace("\\", "/").strip("/")
                    if clean.startswith(f"{user_id}/"):
                        json_data[k] = clean
                    else:
                        json_data[k] = clean
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
                    "workflow_name": workflow_name,
                },
            )
        else:
            new_item = (
                item,
                {
                    "user_id": current_user_id,
                    "username": username,
                    "workflow_name": workflow_name,
                },
            )

        try:
            get_run_log().log_queued(
                prompt_id=prompt_id,
                user_id=current_user_id,
                username=username,
                workflow_name=workflow_name,
                node_count=node_count,
                status="queued",
            )
            print(
                f"[Usgromana] Queue: user={username!r} workflow={workflow_name!r} "
                f"prompt_id={prompt_id!r}"
            )
        except Exception as e:
            print(f"[Usgromana] workflow run log (queue) failed: {e}")

        self.__prompt_queue_put(new_item)

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
                prompt_id = body[1] if isinstance(body, tuple) and len(body) > 1 else None
                if prompt_id:
                    from .workflow_run_log import get_run_log

                    get_run_log().update_status(str(prompt_id), "running")
                uname = meta.get("username") or _resolve_username_for_queue(
                    self.users_db, meta.get("user_id")
                )
                wf = meta.get("workflow_name") or "Unnamed workflow"
                print(f"[Usgromana] Running: user={uname!r} workflow={wf!r} prompt_id={prompt_id!r}")
            except Exception as e:
                print(f"[Usgromana] workflow run log (start) failed: {e}")

            return (entry, task_id)

    def user_queue_task_done(self, item_id, history_result, **kwargs):
        process_item = kwargs.get("process_item")
        status = kwargs.get("status")
        with self.__prompt_queue.mutex:
            item = self.__prompt_queue.currently_running.pop(item_id)
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
                if completed_flag is False:
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
        Deep-copy a history row for the API. For privileged viewers, rewrite
        image/media subfolders to ``<owner_user_id>/...`` so previews resolve
        under the global output root.
        """
        out = copy.deepcopy(entry)
        if "prompt" in out:
            out["prompt"] = sanitize_prompt_tuple_for_api(out["prompt"])
        if not can_view_all:
            return out

        owner = out.get("user_id")
        if not owner:
            return out

        outputs = out.get("outputs")
        if not isinstance(outputs, dict):
            return out

        media_keys = ("images", "gifs", "videos", "audio", "files")
        for node_out in outputs.values():
            if not isinstance(node_out, dict):
                continue
            for key in media_keys:
                items = node_out.get(key)
                if not isinstance(items, list):
                    continue
                for media in items:
                    if not isinstance(media, dict):
                        continue
                    mtype = media.get("type") or "output"
                    if mtype not in ("output", "temp", "input"):
                        continue
                    sub = (media.get("subfolder") or "").replace("\\", "/").strip("/")
                    if sub == owner or sub.startswith(f"{owner}/"):
                        continue
                    media["subfolder"] = f"{owner}/{sub}".strip("/") if sub else str(owner)
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
        with self.__prompt_queue.mutex:
            current_user = self.get_current_user_id()
            can_view_all = self.current_user_can_view_all()
            if can_view_all:
                # Admin/power: clear the entire pending queue
                self.__prompt_queue.queue = []
            else:
                self.__prompt_queue.queue = [
                    i
                    for i in self.__prompt_queue.queue
                    if not (
                        isinstance(i[-1], dict) and i[-1].get("user_id") == current_user
                    )
                ]
            self.server.queue_updated()

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
