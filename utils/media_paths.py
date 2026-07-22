"""Resolve image paths against global Comfy input/output roots (not per-user chroots)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from ..globals import access_control
from .ui_defaults import (
    ASSETS_VISIBILITY_ALLOW_ALL,
    ASSETS_VISIBILITY_DISABLE_ALL,
    ASSETS_VISIBILITY_USER_SPECIFIC,
)


def global_output_directory() -> str:
    return os.path.abspath(access_control._AccessControl__get_output_directory())


def global_input_directory() -> str:
    return os.path.abspath(access_control._AccessControl__get_input_directory())


def global_temp_directory() -> str:
    return os.path.abspath(access_control._AccessControl__get_temp_directory())


def _resolve_under_media_root(
    base: str,
    filename: str | None,
    subfolder: str | None = None,
    *,
    search_all_users: bool | None = None,
) -> str | None:
    """
    Resolve a file under a media root (output/ or temp/) with per-user subfolders.

    Layout:
      base/<username>/file.png
      base/<uuid>/file.png   (legacy)
      base/file.png          (legacy flat)
    """
    if not filename:
        return None

    sub = (subfolder or "").replace("\\", "/").strip("/")
    name = filename.replace("\\", "/").strip("/")
    # Security: no path traversal
    if ".." in name.split("/") or (sub and ".." in sub.split("/")):
        return None

    candidates: list[str] = []
    if sub:
        candidates.append(os.path.join(base, sub, name))
    candidates.append(os.path.join(base, name))

    uid = access_control.get_current_user_id()
    folder_names: set[str] = set()
    if uid:
        try:
            fn = access_control.storage_folder_name(uid)
            if fn:
                folder_names.add(fn)
        except Exception:
            pass
        folder_names.add(str(uid))

    for folder in folder_names:
        if not folder:
            continue
        candidates.append(os.path.join(base, folder, name))
        if sub:
            candidates.append(os.path.join(base, folder, sub, name))

    if search_all_users is None:
        search_all_users = access_control.current_user_can_view_all()

    try:
        for entry in os.listdir(base):
            user_root = os.path.join(base, entry)
            if not os.path.isdir(user_root):
                continue
            if not search_all_users and folder_names and entry not in folder_names:
                continue
            candidates.append(os.path.join(user_root, name))
            if sub:
                candidates.append(os.path.join(user_root, sub, name))
            if sub and sub.split("/")[0] == entry:
                candidates.append(os.path.join(base, sub, name))
    except OSError:
        pass

    seen: set[str] = set()
    for path in candidates:
        norm = os.path.normpath(path)
        if norm in seen:
            continue
        seen.add(norm)
        if not _is_under(norm, base):
            continue
        if os.path.isfile(norm):
            return norm
    return None


def resolve_output_file_path(
    filename: str | None,
    subfolder: str | None = None,
) -> str | None:
    """Find an output image under global output/ + per-user subfolders."""
    return _resolve_under_media_root(
        global_output_directory(), filename, subfolder
    )


def resolve_temp_file_path(
    filename: str | None,
    subfolder: str | None = None,
) -> str | None:
    """
    Find a preview/temp image under global temp/ + per-user subfolders.
    Admin/power see all users' temp previews.
    """
    return _resolve_under_media_root(
        global_temp_directory(), filename, subfolder
    )


def _is_under(path: str, base: str) -> bool:
    path_abs = os.path.normcase(os.path.abspath(path))
    base_abs = os.path.normcase(os.path.abspath(base))
    return path_abs == base_abs or path_abs.startswith(base_abs + os.sep)


def resolve_static_gallery_path(relative_path: str) -> str | None:
    """Map /static_gallery/<rel> to a file under global output/."""
    rel = (relative_path or "").replace("\\", "/").strip("/")
    if not rel or ".." in rel.split("/"):
        return None
    base = global_output_directory()
    candidate = os.path.normpath(os.path.join(base, rel))
    if not _is_under(candidate, base):
        return None
    return candidate if os.path.isfile(candidate) else None


@contextmanager
def gallery_scan_folder_paths(
    visibility_mode: str,
    user_id: str | None = None,
) -> Iterator[None]:
    """
    Temporarily set folder_paths getters for Usgromana Gallery list/serve.

    Uses assets visibility only (who may be listed). NSFW filtering is applied
    separately by the gallery backend via nsfw_media_filter / should_block APIs.

    Per-user folder_paths patching hides global output/; gallery must scan:
    - allow_all: entire global output tree
    - user_specific: output/<user_id>/ only
    """
    import folder_paths

    mode = visibility_mode
    ac = access_control
    saved = (
        folder_paths.get_output_directory,
        folder_paths.get_input_directory,
        folder_paths.get_temp_directory,
    )

    if mode == ASSETS_VISIBILITY_ALLOW_ALL:
        folder_paths.get_output_directory = ac._AccessControl__get_output_directory
        folder_paths.get_input_directory = ac._AccessControl__get_input_directory
        folder_paths.get_temp_directory = ac._AccessControl__get_temp_directory
    elif mode == ASSETS_VISIBILITY_USER_SPECIFIC and user_id:
        base_out = os.path.abspath(ac._AccessControl__get_output_directory())
        base_in = os.path.abspath(ac._AccessControl__get_input_directory())
        user_out = os.path.join(base_out, user_id)
        user_in = os.path.join(base_in, user_id)
        os.makedirs(user_out, exist_ok=True)
        os.makedirs(user_in, exist_ok=True)

        folder_paths.get_output_directory = lambda uo=user_out: uo
        folder_paths.get_input_directory = lambda ui=user_in: ui
        folder_paths.get_temp_directory = ac._AccessControl__get_temp_directory
    # disable_all / no user: leave patched getters as-is

    try:
        yield
    finally:
        folder_paths.get_output_directory = saved[0]
        folder_paths.get_input_directory = saved[1]
        folder_paths.get_temp_directory = saved[2]
