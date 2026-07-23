"""
NSFW media filtering for gallery / Assets — DISABLED (uncensored mode).

Assets visibility (user_specific / allow_all / disable_all) is unchanged and
only controls which users' files appear — never content rating.
"""

from __future__ import annotations

from typing import Any, Sequence


def is_nsfw_filter_active() -> bool:
    return False


def should_hide_media_path(path: str | None) -> bool:
    return False


def _asset_file_path(item: Any) -> str | None:
    ref = getattr(item, "ref", None)
    if ref is None:
        return None
    return getattr(ref, "file_path", None)


def filter_nsfw_asset_items(items: Sequence[Any]) -> list[Any]:
    return list(items)


def asset_detail_is_nsfw_blocked(detail: Any | None) -> bool:
    return False


def asset_download_is_nsfw_blocked(result: Any | None) -> bool:
    return False
