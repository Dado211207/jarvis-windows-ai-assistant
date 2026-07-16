"""Tests for legacy DB migration (app/core/migration.py).

Covers: no legacy DB found, a valid migration, an existing destination
(never overwritten), a corrupted legacy source (never migrated), an
interrupted copy (never leaves a partial destination), and repeated
startups (idempotent — migrates/records at most once).

Isolated the same way as tests/test_paths.py and friends: JARVIS_APPDATA_OVERRIDE
+ a tmp_path chdir, plus paths.installed_program_dir() pointed at a throwaway
directory so the "legacy executable location" is never the real pytest binary.
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import migration


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JARVIS_APPDATA_OVERRIDE", str(tmp_path))
    exe_dir = tmp_path / "Programs" / "JARVIS"
    with patch("app.core.paths.is_frozen", return_value=True), \
         patch("app.core.paths.installed_program_dir", return_value=exe_dir):
        yield exe_dir


def _make_valid_sqlite_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()


def _legacy_path(exe_dir: Path) -> Path:
    return exe_dir / "data" / "jarvis.db"


# --- not applicable outside a frozen build ---

def test_not_applicable_in_dev_mode(_isolated):
    with patch("app.core.paths.is_frozen", return_value=False):
        result = migration.migrate_if_needed()
    assert result["status"] == "not_applicable"
    assert not migration._marker_path().exists()


# --- no legacy DB present ---

def test_no_legacy_found(_isolated):
    result = migration.migrate_if_needed()
    assert result["status"] == "no_legacy_found"
    assert migration._read_marker()["status"] == "no_legacy_found"
    assert not migration.paths.db_path().exists()


# --- happy path: valid legacy DB, no destination yet ---

def test_valid_migration_success(_isolated):
    exe_dir = _isolated
    legacy = _legacy_path(exe_dir)
    _make_valid_sqlite_db(legacy)

    result = migration.migrate_if_needed()

    assert result["status"] == "migrated"
    destination = migration.paths.db_path()
    assert destination.exists()
    assert migration.is_sqlite_db_valid(destination)

    # legacy source is never deleted
    assert legacy.exists()

    # a standalone backup was made
    backups = list(migration.paths.backups_dir().glob("legacy_jarvis_*.db"))
    assert len(backups) == 1
    assert migration.is_sqlite_db_valid(backups[0])

    # no leftover temp file
    assert not destination.with_name(destination.name + ".migrating").exists()


# --- destination already exists: never overwritten ---

def test_destination_already_exists_is_never_overwritten(_isolated):
    exe_dir = _isolated
    legacy = _legacy_path(exe_dir)
    _make_valid_sqlite_db(legacy)

    destination = migration.paths.db_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"pre-existing destination content")

    result = migration.migrate_if_needed()

    assert result["status"] == "skipped_destination_exists"
    assert destination.read_bytes() == b"pre-existing destination content"
    assert legacy.exists()  # legacy also untouched


# --- corrupted legacy DB: never migrated ---

def test_corrupted_legacy_db_is_not_migrated(_isolated):
    exe_dir = _isolated
    legacy = _legacy_path(exe_dir)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"this is not a sqlite database")

    result = migration.migrate_if_needed()

    assert result["status"] == "skipped_corrupted"
    assert not migration.paths.db_path().exists()
    assert legacy.exists()  # never deleted, even though unusable


def test_empty_legacy_file_is_treated_as_corrupted(_isolated):
    exe_dir = _isolated
    legacy = _legacy_path(exe_dir)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.touch()

    result = migration.migrate_if_needed()
    assert result["status"] == "skipped_corrupted"


# --- interrupted copy: destination never left partial ---

def test_interrupted_copy_leaves_no_partial_destination(_isolated):
    exe_dir = _isolated
    legacy = _legacy_path(exe_dir)
    _make_valid_sqlite_db(legacy)

    with patch("app.core.migration._atomic_copy", side_effect=OSError("disk full mid-copy")):
        result = migration.migrate_if_needed()

    assert result["status"] == "failed"
    destination = migration.paths.db_path()
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".migrating").exists()
    assert legacy.exists()  # source untouched despite the failure

    # a backup was still made before the copy step ran
    backups = list(migration.paths.backups_dir().glob("legacy_jarvis_*.db"))
    assert len(backups) == 1


def test_post_copy_integrity_failure_removes_only_destination(_isolated):
    exe_dir = _isolated
    legacy = _legacy_path(exe_dir)
    _make_valid_sqlite_db(legacy)

    with patch("app.core.migration.is_sqlite_db_valid", side_effect=[True, False]):
        result = migration.migrate_if_needed()

    assert result["status"] == "failed_post_copy_integrity"
    assert not migration.paths.db_path().exists()
    assert legacy.exists()


# --- repeated startup: idempotent, at most one attempt ---

def test_repeated_startup_after_successful_migration_is_a_noop(_isolated):
    exe_dir = _isolated
    legacy = _legacy_path(exe_dir)
    _make_valid_sqlite_db(legacy)

    first = migration.migrate_if_needed()
    assert first["status"] == "migrated"

    second = migration.migrate_if_needed()
    assert second["status"] == "already_recorded"

    # migration never runs twice — still exactly one backup
    backups = list(migration.paths.backups_dir().glob("legacy_jarvis_*.db"))
    assert len(backups) == 1


def test_repeated_startup_after_no_legacy_found_is_a_noop(_isolated):
    first = migration.migrate_if_needed()
    assert first["status"] == "no_legacy_found"

    second = migration.migrate_if_needed()
    assert second["status"] == "already_recorded"
    assert second["previous"]["status"] == "no_legacy_found"


def test_repeated_startup_does_not_retry_after_failure(_isolated):
    exe_dir = _isolated
    legacy = _legacy_path(exe_dir)
    _make_valid_sqlite_db(legacy)

    with patch("app.core.migration._atomic_copy", side_effect=OSError("boom")):
        first = migration.migrate_if_needed()
    assert first["status"] == "failed"

    second = migration.migrate_if_needed()
    assert second["status"] == "already_recorded"
    assert second["previous"]["status"] == "failed"


# --- integrity check helper ---

def test_is_sqlite_db_valid_true_for_real_db(tmp_path):
    db_path = tmp_path / "ok.db"
    _make_valid_sqlite_db(db_path)
    assert migration.is_sqlite_db_valid(db_path) is True


def test_is_sqlite_db_valid_false_for_missing_file(tmp_path):
    assert migration.is_sqlite_db_valid(tmp_path / "missing.db") is False


def test_is_sqlite_db_valid_false_for_garbage(tmp_path):
    p = tmp_path / "garbage.db"
    p.write_bytes(b"not a database")
    assert migration.is_sqlite_db_valid(p) is False
