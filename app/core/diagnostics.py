"""Diagnostics — local-only, in-app support information.

Every value here is safe to display and safe to copy into a bug report:
booleans, counts, paths, and version strings. Never includes the Anthropic
API key (masked at most), never includes `.env` contents, never includes
conversation or memory content.
"""

import platform
import subprocess
import sys
from typing import Optional

from app import __phase__, __version__
from app.core import migration, onboarding, paths, runtime_state, secret_store
from app.logging_config import get_logger

logger = get_logger("diagnostics")


def get_report() -> dict:
    from app.config import settings
    from app.core.brain import brain
    from app.core.tool_registry import registry

    db_path = settings.db_path
    log_path = settings.log_file

    return {
        "version": __version__,
        "phase": __phase__,
        "frozen": paths.is_frozen(),
        "os": platform.platform(),
        "python_version": sys.version.split()[0],
        "host": settings.jarvis_host,
        "configured_port": settings.jarvis_port,
        "actual_port": runtime_state.get_actual_port(),
        "paths": {
            "data_dir": str(db_path.parent),
            "logs_dir": str(log_path.parent),
            "db_path": str(db_path),
            "log_file": str(log_path),
        },
        "database": {
            "exists": db_path.exists(),
            "integrity_ok": migration.is_sqlite_db_valid(db_path) if db_path.exists() else None,
        },
        "brain": {
            "configured": brain.is_configured(),
            "provider": settings.jarvis_ai_provider,
            "model": settings.jarvis_ai_model,
        },
        "secure_storage": {
            "backend": secret_store.backend_name(),
            "available": secret_store.is_available(),
            "api_key_stored": secret_store.has_api_key() or settings.has_anthropic_key,
        },
        "onboarding": {
            "complete": onboarding.is_complete(),
        },
        "migration": migration.get_marker(),
        "tools_registered": len(registry),
    }


def open_logs_folder() -> dict:
    """Open the log directory in the OS file manager. No user input, no
    arbitrary path — always exactly paths.logs_dir()."""
    log_dir = paths.logs_dir()
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(["explorer", str(log_dir)], shell=False)  # noqa: S603
        elif system == "Darwin":
            subprocess.Popen(["open", str(log_dir)], shell=False)
        else:
            subprocess.Popen(["xdg-open", str(log_dir)], shell=False)
        logger.info("Opened logs folder: %s", log_dir)
        return {"success": True, "message": f"Opening logs folder: {log_dir}"}
    except Exception as exc:
        logger.warning("Could not open logs folder: %s", exc)
        return {"success": False, "message": f"Could not open logs folder: {exc}"}
