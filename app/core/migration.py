"""Legacy DB migration — moves an alpha-era ZIP install's data\\jarvis.db
(sitting beside the old executable) into the new per-user AppData layout.

Safety rules, all deliberate:
  * Never overwrites an existing destination database — if one is already
    there, both files are left exactly as found.
  * Never deletes the legacy source file, ever, even after a successful
    migration — the user can remove it manually once satisfied.
  * Always makes a standalone backup copy of the legacy source (into
    paths.backups_dir()) before touching anything else.
  * The destination is produced by copying to a temp file in the same
    directory and then an atomic rename — an interruption mid-copy can
    never leave a partial/corrupt file where JARVIS expects its database.
  * Runs at most once per install: a JSON completion marker (any status)
    makes every later startup a fast no-op instead of silently retrying
    (and re-backing-up) forever.
"""

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional

from app.core import paths
from app.logging_config import get_logger

logger = get_logger("migration")

_MARKER_FILENAME = "migration_complete.json"


def _marker_path() -> Path:
    return paths.config_dir() / _MARKER_FILENAME


def _read_marker() -> Optional[dict]:
    path = _marker_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_marker(status: str, source: Optional[Path] = None, error: Optional[str] = None) -> None:
    payload = {"status": status, "source": str(source) if source else None, "completed_at": time.time()}
    if error:
        payload["error"] = error
    _marker_path().write_text(json.dumps(payload), encoding="utf-8")


def is_sqlite_db_valid(path: Path) -> bool:
    """Cheap integrity check. Never raises — any failure means "not valid"."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return bool(row) and row[0] == "ok"
        finally:
            conn.close()
    except Exception:
        return False


def get_marker() -> Optional[dict]:
    """Public read-only accessor for Diagnostics — what migrate_if_needed()
    last recorded, or None if it has never run."""
    return _read_marker()


def find_legacy_db() -> Optional[Path]:
    for candidate in paths.legacy_db_candidates():
        if candidate.exists():
            return candidate
    return None


def _backup_legacy(legacy: Path) -> Path:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = paths.backups_dir() / f"legacy_jarvis_{timestamp}.db"
    shutil.copy2(legacy, backup_path)
    return backup_path


def _atomic_copy(source: Path, destination: Path) -> None:
    tmp_path = destination.with_name(destination.name + ".migrating")
    try:
        shutil.copy2(source, tmp_path)
        tmp_path.replace(destination)  # atomic within the same directory/filesystem
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def migrate_if_needed() -> dict:
    """Idempotent entry point — call once, early, at production startup
    (before anything creates a fresh destination database). Never raises.
    """
    if not paths.is_frozen():
        return {"status": "not_applicable"}

    marker = _read_marker()
    if marker is not None:
        return {"status": "already_recorded", "previous": marker}

    legacy = find_legacy_db()
    if legacy is None:
        _write_marker("no_legacy_found")
        return {"status": "no_legacy_found"}

    destination = paths.db_path()
    if destination.exists():
        logger.info("Legacy DB found at %s but %s already exists — leaving both untouched.", legacy, destination)
        _write_marker("skipped_destination_exists", source=legacy)
        return {"status": "skipped_destination_exists", "source": str(legacy)}

    if not is_sqlite_db_valid(legacy):
        logger.warning("Legacy DB at %s failed an integrity check — not migrating it.", legacy)
        _write_marker("skipped_corrupted", source=legacy)
        return {"status": "skipped_corrupted", "source": str(legacy)}

    try:
        backup_path = _backup_legacy(legacy)
        _atomic_copy(legacy, destination)
        if not is_sqlite_db_valid(destination):
            destination.unlink(missing_ok=True)
            logger.error("Migrated copy of %s failed integrity check after copy.", legacy)
            _write_marker("failed_post_copy_integrity", source=legacy)
            return {"status": "failed_post_copy_integrity", "source": str(legacy)}
    except Exception as exc:
        logger.exception("Legacy DB migration failed for %s", legacy)
        _write_marker("failed", source=legacy, error=str(exc))
        return {"status": "failed", "source": str(legacy), "error": str(exc)}

    logger.info("Migrated legacy DB from %s to %s (backup at %s). Original left in place.", legacy, destination, backup_path)
    _write_marker("migrated", source=legacy)
    return {
        "status": "migrated",
        "source": str(legacy),
        "destination": str(destination),
        "backup": str(backup_path),
    }
