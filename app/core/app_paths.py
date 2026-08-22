"""Authoritative resolver for every mutable path JARVIS reads or writes.

A packaged (PyInstaller/``sys.frozen``) install must never write inside its
own installation directory — Windows per-user installs are frequently
read-only from the app's own perspective, and an upgrade/reinstall must
never risk touching user data by sharing a directory with program files.

Dev-mode behavior (paths relative to the repository) is unchanged from
before this module existed — only packaged mode's *defaults* move under
the current user's local app-data directory. Detection mirrors the
sys.frozen/_MEIPASS check already used by app/api/server.py::_ui_static_dir()
and app/ui/routes.py::_templates_dir() for template/static resolution.

An explicit JARVIS_DB_PATH/JARVIS_LOG_FILE environment variable always
wins in either mode — see app/config.py — matching the override behavior
CI already depends on.
"""

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """True only for a real PyInstaller-frozen process."""
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def app_data_root() -> Path:
    """Base directory for all mutable JARVIS state.

    Packaged: %LOCALAPPDATA%\\JARVIS on Windows. Falls back to an
    XDG-style path when LOCALAPPDATA isn't set (non-Windows dev/CI
    exercising packaged-mode code paths) — never raises.
    Dev: the repository root, matching pre-packaging behavior exactly.
    """
    if not is_frozen():
        return _repo_root()
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
    return base / "JARVIS"


def data_dir() -> Path:
    return app_data_root() / "data"


def config_dir() -> Path:
    return app_data_root() / "config"


def logs_dir() -> Path:
    return data_dir() / "logs"


def models_dir() -> Path:
    return app_data_root() / "models"


def default_db_path() -> Path:
    return data_dir() / "jarvis.db"


def default_log_file() -> Path:
    return logs_dir() / "jarvis.log"


def default_screenshots_dir() -> Path:
    return data_dir() / "screenshots"
