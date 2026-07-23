# ComfyUI runs this before PromptServer is created (see main.py execute_prestartup_script).
# Relative imports are not available here — keep this file self-contained.
#
# Also auto-installs required Python packages (bcrypt, PyJWT, bleach) so a plain
# `git clone` works on a new system without a manual pip step.
import json
import logging
import os
import subprocess
import sys

_log = logging.getLogger("krish_rbac")

_EXT_DIR = os.path.dirname(os.path.abspath(__file__))
_REQUIREMENTS = os.path.join(_EXT_DIR, "requirements.txt")

# import_name -> pip package name
_REQUIRED_PACKAGES = (
    ("bcrypt", "bcrypt"),
    ("jwt", "PyJWT"),
    ("bleach", "bleach"),
)


def _package_missing(import_name: str) -> bool:
    try:
        __import__(import_name)
        return False
    except ImportError:
        return True


def ensure_python_dependencies() -> None:
    """Install missing hard deps into the current ComfyUI Python env."""
    missing = [pip for imp, pip in _REQUIRED_PACKAGES if _package_missing(imp)]
    if not missing:
        return

    print(
        f"[Krish RBAC] Missing packages: {', '.join(missing)}. "
        "Installing into ComfyUI's Python environment…"
    )

    cmds = []
    if os.path.isfile(_REQUIREMENTS):
        cmds.append(
            [sys.executable, "-m", "pip", "install", "-r", _REQUIREMENTS, "--quiet"]
        )
    else:
        cmds.append([sys.executable, "-m", "pip", "install", *missing, "--quiet"])

    # Retry without --quiet for clearer errors if needed
    last_err = None
    for cmd in cmds:
        try:
            subprocess.check_call(cmd)
            still = [pip for imp, pip in _REQUIRED_PACKAGES if _package_missing(imp)]
            if not still:
                print("[Krish RBAC] Dependencies installed successfully.")
                return
            last_err = f"Still missing after install: {', '.join(still)}"
        except Exception as e:
            last_err = e

    # One more attempt: install each package explicitly
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", *missing]
        )
        still = [pip for imp, pip in _REQUIRED_PACKAGES if _package_missing(imp)]
        if not still:
            print("[Krish RBAC] Dependencies installed successfully.")
            return
        last_err = f"Still missing: {', '.join(still)}"
    except Exception as e:
        last_err = e

    print(
        "[Krish RBAC] ERROR: could not install required packages automatically.\n"
        f"  Python: {sys.executable}\n"
        f"  Missing: {', '.join(missing)}\n"
        f"  Detail: {last_err}\n"
        "  Fix manually (use the same Python ComfyUI uses):\n"
        f"    \"{sys.executable}\" -m pip install -r \"{_REQUIREMENTS}\"\n"
        f"  Or: \"{sys.executable}\" -m pip install bcrypt PyJWT bleach"
    )


# Run as early as possible so later imports of this custom node succeed
ensure_python_dependencies()


def _auto_enable_requested() -> bool:
    cfg_path = os.path.join(_EXT_DIR, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f).get("auto_enable_comfy_assets", True)
    except Exception:
        return True


if _auto_enable_requested():
    try:
        from comfy.cli_args import args

        if not getattr(args, "enable_assets", False):
            args.enable_assets = True
            _log.info(
                "[Krish RBAC] Enabled ComfyUI assets system "
                "(no --enable-assets startup flag required)."
            )
        # Krish RBAC owns login; do not enable ComfyUI's built-in user picker (--multi-user UI).
        if getattr(args, "multi_user", False):
            args.multi_user = False
            _log.info(
                "[Krish RBAC] Disabled ComfyUI multi-user login screen "
                "(use Krish RBAC sign-in + Comfy-User header instead)."
            )
    except Exception as e:
        _log.debug("[Krish RBAC] Could not set args.enable_assets: %s", e)

    try:
        from comfy_api import feature_flags

        feature_flags.SERVER_FEATURE_FLAGS["assets"] = True
    except Exception as e:
        _log.debug("[Krish RBAC] Could not set feature_flags.assets: %s", e)
