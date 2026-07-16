"""Centralized production path service — single source of truth for every
on-disk location JARVIS uses (database, logs, cache, backups, config).

Dev/test mode is completely unchanged: callers who never touch this module
keep working exactly as before (repo-relative ``data/`` layout, driven by
``app.config.settings`` and the ``JARVIS_DB_PATH``/``JARVIS_LOG_FILE`` env
vars). This module is consulted only by the production launcher, which — and
only when frozen — seeds those same env vars with per-user AppData paths via
``os.environ.setdefault`` *before* ``app.config`` is first imported. Because
``setdefault`` never overwrites an already-set value, any explicit override
(tests, CI, a developer's own ``.env``) always wins.

Production layout (frozen only):

    %LOCALAPPDATA%\\JARVIS\\
        data\\jarvis.db
        logs\\jarvis.log
        cache\\
        backups\\
        config\\

Dev/test layout (unchanged):

    data\\jarvis.db
    data\\logs\\jarvis.log
"""

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """True only inside a PyInstaller-frozen executable (matches the same
    check already used in app/api/server.py and app/ui/routes.py)."""
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def local_app_data() -> Path:
    """Per-user LocalAppData root.

    Honours JARVIS_APPDATA_OVERRIDE so CI/test-mode can redirect production
    paths at a temporary directory without touching the real profile (see
    docs/WINDOWS_INSTALLER.md, "Windows Runtime Test Mode").
    """
    override = os.environ.get("JARVIS_APPDATA_OVERRIDE")
    if override:
        return Path(override)
    env = os.environ.get("LOCALAPPDATA")
    if env:
        return Path(env)
    # Non-Windows fallback: keeps this module importable on Linux/macOS for
    # pytest and local development. Never reached in a real Windows install.
    return Path.home() / ".local" / "share"


def app_root() -> Path:
    """%LOCALAPPDATA%\\JARVIS — only meaningful when frozen."""
    return local_app_data() / "JARVIS"


def installed_program_dir() -> Path:
    """Where the running executable actually lives (informational, used by
    Diagnostics). Typically %LOCALAPPDATA%\\Programs\\JARVIS post-install."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def _ensure(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    return _ensure(app_root() / "data" if is_frozen() else Path("data"))


def logs_dir() -> Path:
    return _ensure(app_root() / "logs" if is_frozen() else Path("data") / "logs")


def cache_dir() -> Path:
    return _ensure(app_root() / "cache" if is_frozen() else Path("data") / "cache")


def backups_dir() -> Path:
    return _ensure(app_root() / "backups" if is_frozen() else Path("data") / "backups")


def config_dir() -> Path:
    return _ensure(app_root() / "config" if is_frozen() else Path("data") / "config")


def db_path() -> Path:
    return data_dir() / "jarvis.db"


def log_file_path() -> Path:
    return logs_dir() / "jarvis.log"


def onboarding_flag_path() -> Path:
    """Marker file recording that first-run onboarding has completed."""
    return config_dir() / "onboarding_complete.json"


def single_instance_lock_path() -> Path:
    return app_root() / "jarvis.lock" if is_frozen() else Path("data") / "jarvis.lock"


def legacy_db_candidates() -> list:
    """Where an alpha-era ZIP install may have left data\\jarvis.db — beside
    the running executable (the pre-installer CWD-relative layout). Only
    relevant when frozen; returns [] in dev mode since there is nothing to
    migrate away from."""
    if not is_frozen():
        return []
    exe_dir = installed_program_dir()
    return [exe_dir / "data" / "jarvis.db"]


def seed_production_env() -> None:
    """Populate JARVIS_DB_PATH / JARVIS_LOG_FILE with AppData paths.

    Must be called (from the production launcher only) before the first
    `import app.config`. A no-op in dev mode. Never overwrites a value the
    caller/environment already set explicitly.
    """
    if not is_frozen():
        return
    os.environ.setdefault("JARVIS_DB_PATH", str(db_path()))
    os.environ.setdefault("JARVIS_LOG_FILE", str(log_file_path()))
