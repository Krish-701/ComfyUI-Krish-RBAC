# --- START OF FILE utils/sfw_intercept/nsfw_guard.py ---
"""
NSFW / SFW content filtering — DISABLED for uncensored Krish RBAC.

All public APIs are no-ops that never block, never tag, never load models.
Kept as stubs so existing imports continue to work.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Bridge username (still useful for logging / identity elsewhere)
_LATEST_PROMPT_USER = "guest"


def set_latest_prompt_user(username: Optional[str]) -> None:
    """Record who queued the last prompt (identity only — no filtering)."""
    global _LATEST_PROMPT_USER
    _LATEST_PROMPT_USER = (username or "guest").strip() or "guest"


def get_latest_prompt_user() -> str:
    return _LATEST_PROMPT_USER or "guest"


def is_sfw_enforced_for_current_session(quiet: bool = True) -> bool:
    """Always False — no SFW enforcement."""
    return False


def is_sfw_enforced_for_user(username: Optional[str] = None) -> bool:
    """Always False — no SFW enforcement for any user (including guest)."""
    return False


def should_block_image_for_current_user(
    path: Optional[str] = None,
    quiet: bool = True,
    use_cache: bool = True,
) -> bool:
    """Always False — never block images."""
    return False


def check_tensor_nsfw(images_tensor: Any = None) -> bool:
    """Always False — never treat tensors as NSFW."""
    return False


def check_image_path_nsfw(path: Optional[str] = None, username: Optional[str] = None) -> bool:
    """Always False — never treat files as NSFW."""
    return False


def _get_nsfw_pipeline() -> None:
    """No model is loaded."""
    return None


def _get_nsfw_tag(path: str) -> Optional[Dict]:
    return None


def clear_nsfw_tag(path: str) -> bool:
    return False


def clear_all_nsfw_tags() -> int:
    return 0


def set_nsfw_tag_manual(path: str, is_nsfw: bool = False, **kwargs) -> bool:
    return False


def tag_output_images_from_history(history_result: Any = None) -> None:
    """No-op — do not write NSFW metadata."""
    return None


def scan_all_images_in_output_directory(force_rescan: bool = False) -> Dict:
    return {
        "scanned": 0,
        "nsfw_found": 0,
        "errors": 0,
        "total_images": 0,
        "disabled": True,
    }


def fix_incorrectly_cached_tags() -> int:
    return 0


# Quiet notice once at import (no spam)
print("[Krish RBAC] Content filter disabled (uncensored mode) — NSFW/SFW guard is off.")
