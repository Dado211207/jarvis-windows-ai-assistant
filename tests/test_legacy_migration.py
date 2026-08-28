"""Carrying a v0.1 ZIP install's database forward into the installed app.

Exercised against **real SQLite files** on disk, not mocks: every claim
here is about what happens to bytes in a directory, and a mocked
filesystem would prove none of it.

The frozen-mode check is patched rather than the filesystem, so each test
runs the same code the installed application runs — the only thing
pretended is "this process is the packaged executable".
"""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from app.core import legacy_migration


# ---------------------------------------------------------------------------
# Fixtures — a real v0.1 database, and a real AppData root
# ---------------------------------------------------------------------------

def _make_legacy_db(path: Path, memories=("dark roast", "dentist on the 14th")) -> Path:
    """A database shaped like the one v0.1 produced.

    v0.1's schema is deliberately *older* than the current one: it has
    memories and conversations and knows nothing about action_lifecycle.
    Recreating it faithfully is what makes the "apply the current schema
    afterwards" step meaningful.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                tags TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        for content in memories:
            conn.execute("INSERT INTO memories (content) VALUES (?)", (content,))
        conn.execute("INSERT INTO conversations (role, content) VALUES ('user', 'hello')")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def packaged(tmp_path, monkeypatch):
    """Pretend to be the installed executable, with its own AppData root.

    Returns a small namespace with the paths the tests care about.
    """
    app_root = tmp_path / "AppData" / "JARVIS"
    legacy_root = tmp_path / "old install" / "JARVIS"   # a space, on purpose

    monkeypatch.setattr(legacy_migration, "is_frozen", lambda: True)
    monkeypatch.setattr(legacy_migration, "app_data_root", lambda: app_root)
    monkeypatch.setattr(legacy_migration, "config_dir", lambda: app_root / "config")
    monkeypatch.setattr(legacy_migration, "data_dir", lambda: app_root / "data")
    monkeypatch.setattr(legacy_migration, "default_db_path", lambda: app_root / "data" / "jarvis.db")
    monkeypatch.delenv(legacy_migration.LEGACY_DB_ENV, raising=False)

    class _Env:
        root = app_root
        destination = app_root / "data" / "jarvis.db"
        legacy = legacy_root / "data" / "jarvis.db"

    return _Env()


def _rows(path: Path, table: str = "memories"):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute(f"SELECT content FROM {table} ORDER BY id")]
    finally:
        conn.close()


def _point_at(monkeypatch, path: Path) -> None:
    monkeypatch.setenv(legacy_migration.LEGACY_DB_ENV, str(path))


# ---------------------------------------------------------------------------
# No legacy installation
# ---------------------------------------------------------------------------

def test_nothing_happens_in_development_mode(monkeypatch, tmp_path):
    """Dev and CI run against the repository, where "the legacy database"
    would be the developer's own working database."""
    monkeypatch.setattr(legacy_migration, "is_frozen", lambda: False)

    assert legacy_migration.migrate_if_needed()["status"] == "not_applicable"


def test_no_legacy_install_starts_normally(packaged):
    result = legacy_migration.migrate_if_needed()

    assert result["status"] == "no_legacy_found"
    assert not packaged.destination.exists(), "nothing should have been created"


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_a_valid_legacy_database_is_imported(packaged, monkeypatch):
    _make_legacy_db(packaged.legacy)
    _point_at(monkeypatch, packaged.legacy)

    result = legacy_migration.migrate_if_needed()

    assert result["status"] == "migrated"
    assert _rows(packaged.destination) == ["dark roast", "dentist on the 14th"]


def test_the_original_legacy_file_is_never_modified(packaged, monkeypatch):
    _make_legacy_db(packaged.legacy)
    _point_at(monkeypatch, packaged.legacy)
    before_bytes = packaged.legacy.read_bytes()
    before_mtime = packaged.legacy.stat().st_mtime

    legacy_migration.migrate_if_needed()

    assert packaged.legacy.exists(), "the legacy file was removed"
    assert packaged.legacy.read_bytes() == before_bytes, "the legacy file was modified"
    assert packaged.legacy.stat().st_mtime == before_mtime


def test_a_backup_of_the_legacy_file_is_taken(packaged, monkeypatch):
    _make_legacy_db(packaged.legacy)
    _point_at(monkeypatch, packaged.legacy)

    result = legacy_migration.migrate_if_needed()

    backup = Path(result["backup"])
    assert backup.is_file()
    assert backup.read_bytes() == packaged.legacy.read_bytes()


def test_the_current_schema_is_applied_after_importing(packaged, monkeypatch):
    """A v0.1 database predates several tables this version needs. Without
    this step the app would start on a schema that is missing them."""
    _make_legacy_db(packaged.legacy)
    _point_at(monkeypatch, packaged.legacy)

    legacy_migration.migrate_if_needed()

    conn = sqlite3.connect(packaged.destination)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()

    assert {"memories", "conversations", "action_logs", "action_lifecycle"} <= names
    # ...and the imported rows survived the schema step.
    assert _rows(packaged.destination) == ["dark roast", "dentist on the 14th"]


# ---------------------------------------------------------------------------
# Refusing to migrate
# ---------------------------------------------------------------------------

def test_a_corrupt_legacy_database_is_left_alone(packaged, monkeypatch):
    packaged.legacy.parent.mkdir(parents=True, exist_ok=True)
    packaged.legacy.write_bytes(b"this is not a database, it is a text file")
    _point_at(monkeypatch, packaged.legacy)

    result = legacy_migration.migrate_if_needed()

    assert result["status"] == "skipped_invalid_legacy"
    assert packaged.legacy.read_bytes() == b"this is not a database, it is a text file"
    assert not packaged.destination.exists(), "a rejected legacy file must not produce a database"


def test_a_truncated_sqlite_file_is_rejected(packaged, monkeypatch):
    _make_legacy_db(packaged.legacy)
    data = packaged.legacy.read_bytes()
    packaged.legacy.write_bytes(data[: len(data) // 3])
    _point_at(monkeypatch, packaged.legacy)

    assert legacy_migration.migrate_if_needed()["status"] == "skipped_invalid_legacy"


def test_an_unrelated_sqlite_file_is_not_adopted(packaged, monkeypatch):
    """Integrity alone is not enough. Some other program's database in a
    candidate location is intact and is emphatically not our history."""
    packaged.legacy.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(packaged.legacy)
    conn.execute("CREATE TABLE recipes (id INTEGER PRIMARY KEY, title TEXT)")
    conn.commit()
    conn.close()
    _point_at(monkeypatch, packaged.legacy)

    assert legacy_migration.migrate_if_needed()["status"] == "skipped_invalid_legacy"
    assert not packaged.destination.exists()


def test_an_empty_file_is_rejected(packaged, monkeypatch):
    packaged.legacy.parent.mkdir(parents=True, exist_ok=True)
    packaged.legacy.touch()
    _point_at(monkeypatch, packaged.legacy)

    assert legacy_migration.migrate_if_needed()["status"] == "skipped_invalid_legacy"


# ---------------------------------------------------------------------------
# A destination that already has data
# ---------------------------------------------------------------------------

def test_a_destination_with_user_data_is_never_overwritten(packaged, monkeypatch):
    _make_legacy_db(packaged.legacy)
    _point_at(monkeypatch, packaged.legacy)
    _make_legacy_db(packaged.destination, memories=("something the user already saved",))
    existing = packaged.destination.read_bytes()

    result = legacy_migration.migrate_if_needed()

    assert result["status"] == "skipped_destination_has_data"
    assert packaged.destination.read_bytes() == existing
    assert _rows(packaged.destination) == ["something the user already saved"]


def test_records_are_never_merged_or_duplicated(packaged, monkeypatch):
    """Refusing is the whole behaviour. Merging two histories would
    produce rows nobody wrote, in an order nobody chose."""
    _make_legacy_db(packaged.legacy, memories=("old one", "old two"))
    _point_at(monkeypatch, packaged.legacy)
    _make_legacy_db(packaged.destination, memories=("current one",))

    legacy_migration.migrate_if_needed()

    assert _rows(packaged.destination) == ["current one"]


def test_an_empty_destination_is_replaced(packaged, monkeypatch):
    """An initialised-but-empty database is what create_tables() made a
    moment ago, not somebody's data. Refusing to migrate into it would
    make the feature fire only in the case where it is not needed."""
    from db.migrations import create_tables

    _make_legacy_db(packaged.legacy)
    _point_at(monkeypatch, packaged.legacy)
    packaged.destination.parent.mkdir(parents=True, exist_ok=True)
    create_tables(db_path=packaged.destination)
    assert packaged.destination.exists()

    result = legacy_migration.migrate_if_needed()

    assert result["status"] == "migrated"
    assert _rows(packaged.destination) == ["dark roast", "dentist on the 14th"]


# ---------------------------------------------------------------------------
# Interruption and idempotence
# ---------------------------------------------------------------------------

def test_an_interrupted_copy_leaves_no_database_behind(packaged, monkeypatch):
    """An interruption must never leave a half-written file where JARVIS
    expects its database — hence copy-to-temp-then-rename."""
    _make_legacy_db(packaged.legacy)
    _point_at(monkeypatch, packaged.legacy)

    def _explode(source, target):
        Path(target).write_bytes(b"partial")   # a half-written temp file
        raise OSError("disk full")

    monkeypatch.setattr(legacy_migration.shutil, "copy2", _explode)

    result = legacy_migration.migrate_if_needed()

    assert result["status"] == "failed_copy"
    assert not packaged.destination.exists(), "a partial file was left where the database goes"
    assert packaged.legacy.exists()


def test_a_copy_that_arrives_damaged_is_discarded(packaged, monkeypatch):
    _make_legacy_db(packaged.legacy)
    _point_at(monkeypatch, packaged.legacy)

    real_copy = legacy_migration.shutil.copy2

    def _corrupting(source, target):
        target = Path(target)
        if target.name.endswith(".migrating"):
            target.write_bytes(b"corrupted in flight")
            return target
        return real_copy(source, target)

    monkeypatch.setattr(legacy_migration.shutil, "copy2", _corrupting)

    result = legacy_migration.migrate_if_needed()

    assert result["status"] == "failed_verification"
    assert not packaged.destination.exists()


def test_a_second_launch_changes_nothing(packaged, monkeypatch):
    _make_legacy_db(packaged.legacy)
    _point_at(monkeypatch, packaged.legacy)

    first = legacy_migration.migrate_if_needed()
    after_first = packaged.destination.read_bytes()

    second = legacy_migration.migrate_if_needed()

    assert first["status"] == "migrated"
    assert second["status"] == "already_decided"
    assert packaged.destination.read_bytes() == after_first
    assert _rows(packaged.destination) == ["dark roast", "dentist on the 14th"]


def test_ten_launches_do_not_duplicate_a_single_row(packaged, monkeypatch):
    _make_legacy_db(packaged.legacy)
    _point_at(monkeypatch, packaged.legacy)

    for _ in range(10):
        legacy_migration.migrate_if_needed()

    assert _rows(packaged.destination) == ["dark roast", "dentist on the 14th"]
    assert _rows(packaged.destination, "conversations") == ["hello"]


def test_a_refusal_is_also_remembered(packaged, monkeypatch):
    """Not just the success path: re-deciding every launch would mean
    re-backing-up and re-logging forever."""
    packaged.legacy.parent.mkdir(parents=True, exist_ok=True)
    packaged.legacy.write_bytes(b"not a database")
    _point_at(monkeypatch, packaged.legacy)

    assert legacy_migration.migrate_if_needed()["status"] == "skipped_invalid_legacy"
    assert legacy_migration.migrate_if_needed()["status"] == "already_decided"


def test_the_marker_records_what_was_decided(packaged, monkeypatch):
    _make_legacy_db(packaged.legacy)
    _point_at(monkeypatch, packaged.legacy)

    legacy_migration.migrate_if_needed()

    marker = json.loads(legacy_migration.marker_path().read_text(encoding="utf-8"))
    assert marker["status"] == "migrated"
    assert legacy_migration.last_result()["status"] == "migrated"


# ---------------------------------------------------------------------------
# Paths: spaces and non-ASCII
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("folder", [
    "old install",              # a space
    "Ünïcødé Ñame",             # non-ASCII, as a Windows username can be
    "用户",                      # non-Latin
    "dossier avec espaces et é",
])
def test_paths_with_spaces_and_non_ascii_work(tmp_path, monkeypatch, folder):
    app_root = tmp_path / folder / "AppData" / "JARVIS"
    legacy = tmp_path / folder / "JARVIS" / "data" / "jarvis.db"

    monkeypatch.setattr(legacy_migration, "is_frozen", lambda: True)
    monkeypatch.setattr(legacy_migration, "app_data_root", lambda: app_root)
    monkeypatch.setattr(legacy_migration, "config_dir", lambda: app_root / "config")
    monkeypatch.setattr(legacy_migration, "data_dir", lambda: app_root / "data")
    monkeypatch.setattr(legacy_migration, "default_db_path", lambda: app_root / "data" / "jarvis.db")

    _make_legacy_db(legacy)
    monkeypatch.setenv(legacy_migration.LEGACY_DB_ENV, str(legacy))

    result = legacy_migration.migrate_if_needed()

    assert result["status"] == "migrated", f"{folder!r} broke the migration"
    assert _rows(app_root / "data" / "jarvis.db") == ["dark roast", "dentist on the 14th"]


# ---------------------------------------------------------------------------
# Where it looks
# ---------------------------------------------------------------------------

def test_the_candidate_list_is_bounded_and_never_scans(monkeypatch):
    """Every candidate is one existence check. A glob or a directory walk
    would turn a startup step into a disk scan."""
    import inspect

    source = inspect.getsource(legacy_migration)
    for forbidden in ("rglob", ".glob(", "os.walk", "iterdir"):
        assert forbidden not in source, f"legacy_migration reaches for {forbidden}"


def test_the_override_env_var_wins_and_is_the_only_candidate(monkeypatch, tmp_path):
    target = tmp_path / "somewhere" / "else.db"
    monkeypatch.setenv(legacy_migration.LEGACY_DB_ENV, str(target))

    assert legacy_migration.legacy_db_candidates() == [target]


def test_the_documented_v0_1_location_is_a_candidate(monkeypatch):
    """v0.1's QUICKSTART said to extract to C:\\JARVIS. That is where its
    data\\jarvis.db would be, and guessing anywhere else would be
    inventing a path."""
    monkeypatch.delenv(legacy_migration.LEGACY_DB_ENV, raising=False)
    # Patched through the module's own helper rather than os.name, which
    # decides what Path() constructs for the whole process.
    monkeypatch.setattr(legacy_migration, "_is_windows", lambda: True)
    monkeypatch.setenv("SystemDrive", "C:")

    candidates = [str(p).replace("\\", "/") for p in legacy_migration.legacy_db_candidates()]

    assert any(c.endswith("JARVIS/JARVIS/data/jarvis.db") for c in candidates), candidates
    assert any(c.endswith("JARVIS/data/jarvis.db") for c in candidates)


def test_a_candidate_that_cannot_be_stat_ed_is_skipped(packaged, monkeypatch):
    """A disconnected drive or a permission wall is not a candidate, and
    is certainly not a crash."""
    monkeypatch.setattr(
        legacy_migration, "legacy_db_candidates",
        lambda: [Path("Z:/gone/data/jarvis.db"), packaged.legacy],
    )
    _make_legacy_db(packaged.legacy)

    assert legacy_migration.migrate_if_needed()["status"] == "migrated"


# ---------------------------------------------------------------------------
# Failing safely
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("helper", ["validate_legacy_db", "find_legacy_db", "destination_has_user_data"])
def test_nothing_here_can_stop_jarvis_starting(packaged, monkeypatch, helper):
    """Every failure path returns a status.

    This runs on the startup path of a windowed build with no console,
    where an unhandled exception becomes a modal dialog nobody can
    dismiss. Optional data is not worth that, so the guarantee is
    enforced rather than assumed of each helper.
    """
    _make_legacy_db(packaged.legacy)
    _point_at(monkeypatch, packaged.legacy)

    def _explode(*args, **kwargs):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(legacy_migration, helper, _explode)

    result = legacy_migration.migrate_if_needed()

    assert result["status"] == "failed_unexpectedly"
    assert result["error"] == "RuntimeError"


def test_an_unexpected_failure_is_remembered_rather_than_retried(packaged, monkeypatch):
    _make_legacy_db(packaged.legacy)
    _point_at(monkeypatch, packaged.legacy)
    monkeypatch.setattr(
        legacy_migration, "validate_legacy_db",
        lambda path: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert legacy_migration.migrate_if_needed()["status"] == "failed_unexpectedly"
    monkeypatch.setattr(legacy_migration, "validate_legacy_db", lambda path: True)
    assert legacy_migration.migrate_if_needed()["status"] == "already_decided"


def test_a_failure_message_does_not_expose_the_data(packaged, monkeypatch, caplog):
    packaged.legacy.parent.mkdir(parents=True, exist_ok=True)
    packaged.legacy.write_bytes(b"secret-looking content nobody should see in a log")
    _point_at(monkeypatch, packaged.legacy)

    with caplog.at_level("DEBUG"):
        legacy_migration.migrate_if_needed()

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret-looking content" not in combined
