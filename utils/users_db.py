import bcrypt
import json
import os
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .json_utils import save_json_file


class UsersDB:
    def __init__(self, database: str | Path):
        self.database = database

        # Stored as: { user_id: { username, password, admin?, groups? } }
        self.users: dict = {}
        self.admin_user: tuple[str | None, dict] = (None, {})

        self._database_hash: str | None = None

        self.load_users()

    # ----------------------------
    # Password + file helpers
    # ----------------------------

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt."""
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    def calculate_file_hash(self) -> str:
        """Calculate the SHA256 hash of the database file."""
        if os.path.exists(self.database):
            with open(self.database, "rb") as f:
                file_data = f.read()
                return hashlib.sha256(file_data).hexdigest()
        return ""

    # ----------------------------
    # Load / save
    # ----------------------------

    def load_users(self) -> dict:
        """Load users from the database if it has changed."""
        current_hash = self.calculate_file_hash()
        if current_hash != self._database_hash:
            if os.path.exists(self.database):
                with open(self.database, "r", encoding="utf-8") as f:
                    try:
                        self.users = json.load(f)
                    except json.JSONDecodeError:
                        self.users = {}
                # 🔧 Migration / safety: ensure groups exist for all users
                self._ensure_groups_schema()
                self._database_hash = self.calculate_file_hash()
            else:
                self.users = {}
        return self.users

    def save_users(self, users: dict) -> None:
        """Save users to the database and update the hash."""
        save_json_file(self.database, users)
        self._database_hash = self.calculate_file_hash()

    # ----------------------------
    # Schema helpers
    # ----------------------------

    def _ensure_groups_schema(self) -> None:
        """
        Ensure every user has a 'groups' list.
        - If user has admin flag but no groups → groups = ["admin"]
        - Else if no groups → groups = ["user"]
        """
        changed = False
        for uid, user in list(self.users.items()):
            # Normalize
            if "groups" not in user or not isinstance(user["groups"], list) or not user["groups"]:
                if user.get("admin"):
                    user["groups"] = ["admin"]
                else:
                    user["groups"] = ["user"]
                changed = True

        if changed:
            self.save_users(self.users)

    def _has_admin(self) -> bool:
        """Return True if any user has admin rights (admin flag OR admin group)."""
        self.load_users()
        for _uid, user in self.users.items():
            if user.get("admin"):
                return True
            groups = [g.lower() for g in user.get("groups", [])]
            if "admin" in groups:
                return True
        return False

    # ----------------------------
    # Public API
    # ----------------------------

    def add_user(
        self,
        id: str,
        username: str,
        password: str,
        admin: bool,
        email: str | None = None,
        groups: list | None = None,
    ) -> None:
        """
        Add a user to the database.

        Rules:
        - If this is the very first user and no admin exists → force admin + groups=["admin"]
        - Otherwise:
          - if groups is provided → use it (admin flag derived if "admin" in groups)
          - elif admin=True → groups=["admin"]
          - else → groups=["user"]
        - Optional email is stored for login-by-email
        """
        self.load_users()

        # Determine if we already have an admin
        has_admin = self._has_admin()

        # First user and no admin yet? Force admin.
        if not has_admin and len(self.users) == 0:
            admin = True
            groups = ["admin"]

        # Assign groups based on admin flag / explicit groups
        if groups is not None:
            groups = [str(g).lower().strip() for g in groups if str(g).strip()]
            if not groups:
                groups = ["admin"] if admin else ["user"]
            admin = bool(admin) or ("admin" in groups)
            if admin and "admin" not in groups:
                groups = ["admin"]
        elif admin:
            groups = ["admin"]
        else:
            groups = ["user"]

        user = {
            "username": username,
            "password": self.hash_password(password),
            "admin": bool(admin),
            "groups": groups,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if email:
            user["email"] = str(email).strip().lower()

        self.users[id] = user
        self.save_users(self.users)

    def get_user(self, username: str = "", user_id: str = "") -> tuple[str | None, dict]:
        """
        Retrieve a user by username, email, or user_id.
        Always returns (id, user_dict_or_empty).
        Login identifier may be username OR email (case-insensitive for email).
        """
        self.load_users()

        if user_id:
            user = self.users.get(user_id)
            if user is not None:
                return user_id, user
            return None, {}

        if not username:
            return None, {}

        key = str(username).strip()
        key_lower = key.lower()

        # Exact username match first
        for uid, user_data in self.users.items():
            if user_data.get("username") == key:
                return uid, user_data

        # Case-insensitive username
        for uid, user_data in self.users.items():
            if str(user_data.get("username", "")).lower() == key_lower:
                return uid, user_data

        # Email match (case-insensitive)
        for uid, user_data in self.users.items():
            email = user_data.get("email")
            if email and str(email).strip().lower() == key_lower:
                return uid, user_data

        return None, {}

    def email_exists(self, email: str, exclude_user_id: str | None = None) -> bool:
        """Return True if another account already uses this email."""
        if not email:
            return False
        self.load_users()
        target = str(email).strip().lower()
        for uid, user_data in self.users.items():
            if exclude_user_id and uid == exclude_user_id:
                continue
            stored = user_data.get("email")
            if stored and str(stored).strip().lower() == target:
                return True
        return False

    def check_username_password(self, username: str, password: str) -> bool:
        """Check credentials. ``username`` may be username or email."""
        user_id, user_data = self.get_user(username)
        if not user_id or not user_data:
            return False
        if user_data.get("disabled"):
            return False

        return bcrypt.checkpw(
            password.encode("utf-8"), user_data["password"].encode("utf-8")
        )

    def authenticate(self, login: str, password: str) -> tuple[str | None, dict]:
        """
        Authenticate with username OR email + password.
        Returns (user_id, user_dict) on success, else (None, {}).
        Disabled accounts never authenticate.
        """
        if not login or not password:
            return None, {}
        user_id, user_data = self.get_user(login)
        if not user_id or not user_data:
            return None, {}
        if user_data.get("disabled"):
            return None, {"_disabled": True}
        try:
            ok = bcrypt.checkpw(
                password.encode("utf-8"), user_data["password"].encode("utf-8")
            )
        except Exception:
            return None, {}
        if not ok:
            return None, {}
        return user_id, user_data

    def set_password(
        self,
        username: str,
        new_password: str,
        *,
        force_change: bool = False,
    ) -> bool:
        """
        Reset a user's password by username (or email). Returns True on success.
        If force_change=True, user must change password on next login.
        """
        if username is None or new_password is None:
            return False
        user_id, user_data = self.get_user(username)
        if not user_id or not user_data:
            return False
        self.load_users()
        if user_id not in self.users:
            return False
        self.users[user_id]["password"] = self.hash_password(str(new_password))
        if force_change:
            self.users[user_id]["must_change_password"] = True
        else:
            self.users[user_id]["must_change_password"] = False
        self.save_users(self.users)
        return True

    def clear_must_change_password(self, username: str) -> bool:
        user_id, _ = self.get_user(username)
        if not user_id:
            return False
        self.load_users()
        if user_id not in self.users:
            return False
        self.users[user_id]["must_change_password"] = False
        self.save_users(self.users)
        return True

    def set_disabled(self, username: str, disabled: bool) -> bool:
        """Soft-ban: disable login without deleting the account."""
        user_id, user_data = self.get_user(username)
        if not user_id or not user_data:
            return False
        if (user_data.get("username") or "").lower() == "guest":
            return False
        self.load_users()
        if user_id not in self.users:
            return False
        self.users[user_id]["disabled"] = bool(disabled)
        self.save_users(self.users)
        return True

    def is_disabled(self, username: str = "", user_id: str = "") -> bool:
        uid, rec = self.get_user(username=username, user_id=user_id)
        if not rec:
            return False
        return bool(rec.get("disabled"))

    def get_admin_user(self) -> tuple[str | None, dict] | None:
        """
        Get the admin user from the database.
        Returns (id, user_dict) or (None, {}) if none.
        """
        self.load_users()
        self.admin_user = (None, {})

        for uid, user_data in self.users.items():
            groups = [g.lower() for g in user_data.get("groups", [])]
            if user_data.get("admin") or "admin" in groups:
                self.admin_user = (uid, user_data)
                break

        return self.admin_user
