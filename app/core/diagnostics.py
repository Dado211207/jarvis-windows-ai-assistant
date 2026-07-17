"""Diagnostics — local-only, in-app support information.

Every value in get_report() is safe to *display* on the Diagnostics page
itself: booleans, counts, paths, and version strings. Never includes the
Anthropic API key (masked at most), never includes `.env` contents, never
includes conversation or memory content. "Safe to display" is not the same
as "safe to paste into a public bug report or chat" though — the real
paths in get_report() embed the Windows username
(``C:\\Users\\<name>\\AppData\\...``), and ``migration.get_marker()``'s
``source``/``error`` fields can carry a legacy-DB path or a raw Python
exception message with the same problem. get_report_text() is the
separate, redacted rendering meant for copying/sharing — see redact_text().
"""

import platform
import subprocess
import sys

from app import __phase__, __version__
from app.core import migration, onboarding, paths, runtime_state, secret_store
from app.core.redact import redact_text
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


def get_report_text() -> str:
    """The redacted, copy/paste-safe rendering of get_report() — same
    fields, same order as the Diagnostics page's on-screen display, but
    every value passes through redact_text() before being joined. This is
    what the "Copy report" button actually copies (see
    GET /diagnostics/report-text); the raw get_report() dict is only ever
    used for the page's own on-screen fields, never exported as-is."""
    import json

    report = get_report()
    migration_marker = report["migration"]
    if migration_marker:
        # Redact each string field in its natural (unescaped) form first —
        # json.dumps() below would otherwise double every backslash in a
        # Windows path, which _PATH_USERNAME_RE also handles, but there's
        # no reason to rely on that alone when redacting before escaping is
        # just as easy and more direct.
        redacted_marker = {
            k: (redact_text(v) if isinstance(v, str) else v) for k, v in migration_marker.items()
        }
        migration_line = json.dumps(redacted_marker)
    else:
        migration_line = "not applicable"

    lines = [
        "JARVIS Diagnostics Report",
        f"Version: {report['version']} ({report['phase']})",
        f"Installed app: {report['frozen']}",
        f"OS: {report['os']}",
        f"Python: {report['python_version']}",
        f"Onboarding complete: {report['onboarding']['complete']}",
        "",
        f"Host: {report['host']}",
        f"Configured port: {report['configured_port']}",
        f"Actual port: {report['actual_port']}",
        f"AI provider: {report['brain']['provider']} (model: {report['brain']['model']})",
        f"AI configured: {report['brain']['configured']}",
        f"Tools registered: {report['tools_registered']}",
        "",
        f"Database path: {redact_text(report['paths']['db_path'])}",
        f"Database exists: {report['database']['exists']}",
        f"Database integrity ok: {report['database']['integrity_ok']}",
        f"Log folder: {redact_text(report['paths']['logs_dir'])}",
        f"Secure key storage backend: {report['secure_storage']['backend']}",
        f"Secure key storage available: {report['secure_storage']['available']}",
        f"API key configured: {report['secure_storage']['api_key_stored']}",
        f"Legacy DB migration: {migration_line}",
    ]
    # Final whole-text pass: redact_text is idempotent on already-redacted
    # substrings, so re-running it over the joined text costs nothing and
    # catches anything that slipped through field-by-field (e.g. a value
    # that itself embeds a path deeper than the top-level fields above).
    return redact_text("\n".join(lines))


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
        return {"success": False, "message": f"Could not open logs folder: {redact_text(str(exc))}"}
