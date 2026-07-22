# --- START OF FILE utils/bootstrap.py ---
import os
import uuid
from ..constants import USERS_FILE, GROUPS_CONFIG_FILE, DEFAULT_GROUP_CONFIG_PATH
from .ui_defaults import ensure_ui_defaults_config
from .json_utils import load_json_file, save_json_file
from .admin_logic import patch_user_group
from ..globals import logger, users_db

# Hardcoded default super-admin for this deployment (create if missing).
DEFAULT_ADMIN_USERNAME = "Krish"
DEFAULT_ADMIN_PASSWORD = "701702@Advik$"

def load_default_groups():
    cfg = load_json_file(DEFAULT_GROUP_CONFIG_PATH, None)
    if cfg is None:
        logger.error("[Usgromana] Missing default_group_config.json; using built-in fallback!")
        return {
            "admin": { "can_run": True, "can_upload": True, "can_access_manager": True, "can_access_api": True, "can_see_restricted_settings": True },
            "power": { "can_run": True, "can_upload": True, "can_access_manager": True, "can_access_api": True, "can_see_restricted_settings": False },
            "user": { "can_run": True, "can_upload": True, "can_access_manager": False, "can_access_api": True, "can_see_restricted_settings": False },
            "guest": { "can_run": False, "can_upload": False, "can_access_manager": False, "can_access_api": True, "can_see_restricted_settings": False },
        }
    return cfg

def ensure_groups_config():
    """Merge missing roles/keys into groups config. Never deletes the users/ directory."""
    default_cfg = load_default_groups()
    current = load_json_file(GROUPS_CONFIG_FILE, {})
    changed = False

    # Add missing groups
    for role, perms in default_cfg.items():
        if role not in current:
            current[role] = perms
            changed = True

    # Add missing permission keys
    for role, perms in default_cfg.items():
        for key, value in perms.items():
            if key not in current[role]:
                current[role][key] = value
                changed = True

    if changed:
        save_json_file(GROUPS_CONFIG_FILE, current)

    ensure_ui_defaults_config()

def ensure_guest_user():
    try:
        guest_id, guest_rec = users_db.get_user("guest")
    except Exception as e:
        logger.error(f"[Usgromana] Error checking guest user: {e}")
        return

    if guest_id is not None:
        patch_user_group("guest", ["guest"], False)
        return

    try:
        random_password = str(uuid.uuid4())
        new_guest_id = str(uuid.uuid4())
        users_db.add_user(new_guest_id, "guest", random_password, False)
        patch_user_group("guest", ["guest"], False)
        logger.info("[Usgromana] Created default 'guest' user")
    except Exception as e:
        logger.error(f"[Usgromana] Error creating guest user: {e}")


def ensure_default_admin():
    """
    Ensure the hardcoded default admin account exists:
      username: Krish
      password: 701702@Advik$

    - Creates the account if missing.
    - Ensures admin role if the account exists.
    - Does not reset password every startup (use admin Reset PW if needed).
    """
    try:
        uid, rec = users_db.get_user(username=DEFAULT_ADMIN_USERNAME)
    except Exception as e:
        logger.error(f"[Usgromana] Error checking default admin: {e}")
        return

    if uid is not None and rec:
        # Keep as admin; clear disabled flag so default admin always works
        try:
            groups = [g.lower() for g in (rec.get("groups") or [])]
            if "admin" not in groups or not rec.get("admin"):
                patch_user_group(DEFAULT_ADMIN_USERNAME, ["admin"], True)
                logger.info(
                    f"[Usgromana] Ensured '{DEFAULT_ADMIN_USERNAME}' has admin role"
                )
            if rec.get("disabled"):
                users_db.set_disabled(DEFAULT_ADMIN_USERNAME, False)
                logger.info(
                    f"[Usgromana] Re-enabled default admin '{DEFAULT_ADMIN_USERNAME}'"
                )
            # Clear forced password-change so login is not blocked
            if rec.get("must_change_password"):
                users_db.clear_must_change_password(DEFAULT_ADMIN_USERNAME)
        except Exception as e:
            logger.error(f"[Usgromana] Error updating default admin: {e}")
        return

    try:
        new_id = str(uuid.uuid4())
        users_db.add_user(
            new_id,
            DEFAULT_ADMIN_USERNAME,
            DEFAULT_ADMIN_PASSWORD,
            True,
        )
        patch_user_group(DEFAULT_ADMIN_USERNAME, ["admin"], True)
        # Ensure password is the hardcoded one (add_user already set it)
        users_db.set_password(
            DEFAULT_ADMIN_USERNAME,
            DEFAULT_ADMIN_PASSWORD,
            force_change=False,
        )
        users_db.clear_must_change_password(DEFAULT_ADMIN_USERNAME)
        logger.info(
            f"[Usgromana] Created default admin user '{DEFAULT_ADMIN_USERNAME}'"
        )
        print(
            f"[Krish RBAC] Default admin ready — username: {DEFAULT_ADMIN_USERNAME}"
        )
    except Exception as e:
        logger.error(f"[Usgromana] Error creating default admin: {e}")
        print(f"[Krish RBAC] Failed to create default admin: {e}")