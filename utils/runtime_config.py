"""Read/write mutable extension settings stored in config.json."""
from ..constants import CONFIG_FILE_PATH
from .json_utils import load_json_file, save_json_file


def get_blacklist_after_attempts() -> int:
    cfg = load_json_file(CONFIG_FILE_PATH, {})
    raw = cfg.get("blacklist_after_attempts", 0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def set_blacklist_after_attempts(value: int) -> int:
    normalized = max(0, int(value))
    cfg = load_json_file(CONFIG_FILE_PATH, {})
    cfg["blacklist_after_attempts"] = normalized
    save_json_file(CONFIG_FILE_PATH, cfg)
    return normalized
