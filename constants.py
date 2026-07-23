# --- START OF FILE constants.py ---
import os
import json
import secrets
import warnings

# --- Base Directories ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(CURRENT_DIR, "web")

# NOTE: If your html files are directly in web/, remove the 'html' part below
# But based on standard structure, they should be in web/html/
HTML_DIR = os.path.join(WEB_DIR, "html") 
CSS_DIR = os.path.join(WEB_DIR, "css")
JS_DIR = os.path.join(WEB_DIR, "js")
ASSETS_DIR = os.path.join(WEB_DIR, "assets")

# --- Load config.json ---
CONFIG_FILE_PATH = os.path.join(CURRENT_DIR, "config.json")
def _load_config(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            warnings.warn(f"[Krish RBAC] Failed to load config from {path}: {e}")
    return {}

config_data = _load_config(CONFIG_FILE_PATH)

from .utils.users_storage import ensure_users_data_layout, resolve_users_database_path

# --- Files & Paths (paths from config.json; defaults for backward compatibility) ---
_users_db_configured = os.path.join(
    CURRENT_DIR, config_data.get("users_db", "users/users.json")
)
USERS_FILE = resolve_users_database_path(_users_db_configured, CURRENT_DIR)
GROUPS_CONFIG_FILE = os.path.join(CURRENT_DIR, "users", "usgromana_groups.json")
DEFAULT_GROUP_CONFIG_PATH = os.path.join(CURRENT_DIR, "users", "defaults", "default_group_config.json")
DEFAULT_UI_DEFAULTS_PATH = os.path.join(CURRENT_DIR, "users", "defaults", "default_ui_defaults.json")
UI_DEFAULTS_FILE = os.path.join(CURRENT_DIR, "users", "usgromana_ui_defaults.json")
WHITELIST_FILE = os.path.join(CURRENT_DIR, config_data.get("whitelist", "users/whitelist.txt"))
BLACKLIST_FILE = os.path.join(CURRENT_DIR, config_data.get("blacklist", "users/blacklist.txt"))
LOG_FILE = os.path.join(CURRENT_DIR, config_data.get("log", "usgromana.log"))
SECRET_KEY_FILE = os.path.join(CURRENT_DIR, "users", ".secret_key")

# Create users/ layout only; never delete or replace live data files.
ensure_users_data_layout(CURRENT_DIR, touch_files=[WHITELIST_FILE, BLACKLIST_FILE])


def _resolve_secret_key() -> str:
    """
    Resolve JWT signing key in order:
      1. Environment variable (config secret_key_env, default SECRET_KEY)
      2. config.json \"secret_key\"
      3. Persisted users/.secret_key (stable across restarts after git clone)
      4. Generate + save a new key
    """
    env_name = config_data.get("secret_key_env", "SECRET_KEY") or "SECRET_KEY"
    key = (os.getenv(env_name) or "").strip()
    if key:
        return key

    cfg_key = config_data.get("secret_key")
    if isinstance(cfg_key, str) and cfg_key.strip():
        return cfg_key.strip()

    try:
        if os.path.isfile(SECRET_KEY_FILE):
            with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
                stored = f.read().strip()
            if stored:
                return stored
    except OSError as e:
        warnings.warn(f"[Krish RBAC] Could not read secret key file: {e}")

    # First run on this machine — generate once and persist so restarts keep sessions
    key = secrets.token_hex(64)
    try:
        os.makedirs(os.path.dirname(SECRET_KEY_FILE), exist_ok=True)
        with open(SECRET_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(key)
        try:
            os.chmod(SECRET_KEY_FILE, 0o600)
        except OSError:
            pass
        print(
            f"[Krish RBAC] Generated and saved SECRET_KEY to {SECRET_KEY_FILE} "
            "(set env SECRET_KEY to override)."
        )
    except OSError as e:
        warnings.warn(
            f"[Krish RBAC] SECRET_KEY not set and could not persist key file ({e}). "
            "Sessions may reset on restart."
        )
    return key


# --- Configuration Values ---
LOG_LEVELS = config_data.get("log_levels", ["INFO"])
SECRET_KEY = _resolve_secret_key()

TOKEN_EXPIRE_MINUTES = 60 * config_data.get("access_token_expiration_hours", 12)
MAX_TOKEN_EXPIRE_MINUTES = 60 * config_data.get("max_access_token_expiration_hours", 8760)
TOKEN_ALGORITHM = "HS256"

BLACKLIST_AFTER_ATTEMPTS = config_data.get("blacklist_after_attempts", 0)
FREE_MEMORY_ON_LOGOUT = config_data.get("free_memory_on_logout", True)
FORCE_HTTPS = config_data.get("force_https", False)
# Config key kept as "seperate_users" for backward compatibility
SEPARATE_USERS = config_data.get("seperate_users", True)
MANAGER_ADMIN_ONLY = config_data.get("manager_admin_only", True)
MATCH_HEADERS = {"X-Forwarded-Proto": "https"}

# Per-user concurrent queue cap (pending + running). 0 = unlimited.
MAX_QUEUE_JOBS_PER_USER = int(config_data.get("max_queue_jobs_per_user", 2) or 0)
_raw_exempt = config_data.get("queue_limit_exempt_roles", ["admin"])
if isinstance(_raw_exempt, str):
    QUEUE_LIMIT_EXEMPT_ROLES = {g.strip().lower() for g in _raw_exempt.split(",") if g.strip()}
elif isinstance(_raw_exempt, (list, tuple, set)):
    QUEUE_LIMIT_EXEMPT_ROLES = {str(g).strip().lower() for g in _raw_exempt if str(g).strip()}
else:
    QUEUE_LIMIT_EXEMPT_ROLES = {"admin", "power"}