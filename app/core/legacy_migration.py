"""Bringing a v0.1 ZIP install's data forward into the installed app.

Somebody who used the v0.1 alpha ZIP has memories, chat history and an
action log in a SQLite file. Installing v0.2 gave them an empty database
and left the old one sitting where it was — untouched, but unread. This
carries it over.

**Where the old database actually is.** v0.1's `app/config.py` declared
`jarvis_db_path: str = "data/jarvis.db"` — a *relative* path, resolved
against the working directory of the running process. The ZIP contained
a `JARVIS\\` folder with `JARVIS.exe` inside it, and double-clicking that
executable makes its own folder the working directory, so the database
landed at `<wherever it was extracted>\\JARVIS\\data\\jarvis.db`.

That is the honest answer, and it has an uncomfortable consequence:
**v0.1 had no fixed install location**, so there is no single path to
look at. `legacy_db_candidates()` below is a bounded list of the places
v0.1's own QUICKSTART told people to extract to, plus the two folders a
downloaded ZIP is extracted into by default. Every entry is one
`exists()` call. Nothing walks a directory tree, nothing scans a disk,
and nothing looks outside the current user's own profile except the
`C:\\JARVIS` path that v0.1's documentation named explicitly.

For anyone whose copy is somewhere else, `JARVIS_LEGACY_DB` names the
file directly. That is the complete answer where the guesswork is not.

**Everything here is conservative, and none of it is clever:**

* The legacy file is only ever *read*. It is never moved, deleted or
  modified — not even after a successful migration. Somebody who wants
  it gone can delete it themselves once they are satisfied.
* A destination that already holds user data is never overwritten. An
  *empty* destination is fine to replace: that is a schema `create_tables()`
  made a moment ago, not somebody's data.
* The copy goes to a temporary name in the destination directory and is
  then atomically renamed, so an interruption cannot leave a half-written
  file where JARVIS expects its database.
* A backup of the legacy file is taken before anything else happens.
* The legacy file is validated before it is trusted: a SQLite integrity
  check, and a look for the tables a JARVIS database actually has. A
  corrupt or unrelated file is left exactly where it is.
* The current schema is applied afterwards, because a v0.1 database
  predates several tables this version needs.
* A marker records that the decision was made, so this runs at most once
  and repeated launches cannot duplicate anything.
* Nothing raises. A failure here must never stop JARVIS starting; it
  degrades to "start with an empty database", which is what would have
  happened anyway.
"""

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import List, Optional

from app.core.app_paths import app_data_root, config_dir, data_dir, default_db_path, is_frozen
from app.logging_config import get_logger

logger = get_logger("legacy_migration")

MARKER_FILENAME = "legacy_migration.json"

# An environment variable rather than a setting: this is answered once,
# by somebody who knows where their old install is, and never again.
LEGACY_DB_ENV = "JARVIS_LEGACY_DB"

# Tables a real JARVIS database has. `memories` is the one v0.1 certainly
# had; requiring it stops an unrelated .db file being adopted.
_REQUIRED_LEGACY_TABLES = ("memories",)

# Tables whose contents count as "the user has data here". Deliberately
# not settings or schema rows — an empty-but-initialised database is
# something to replace, not something to protect.
_USER_DATA_TABLES = ("memories", "conversations", "action_logs")


def _is_windows() -> bool:
    """Its own function so a test can ask "what would this list be on
    Windows?" without patching `os.name`, which changes what `Path()`
    constructs for the rest of the process."""
    return os.name == "nt"


def marker_path() -> Path:
    return config_dir() / MARKER_FILENAME


def _read_marker() -> Optional[dict]:
    try:
        return json.loads(marker_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a missing or unreadable marker means "not decided yet"
        return None


def _write_marker(status: str, source: Optional[Path] = None, detail: str = "") -> None:
    """Record the decision. Never raises — a marker that cannot be written
    means this runs again next time, which is safe because every outcome
    below is idempotent."""
    payload = {
        "status": status,
        "source": str(source) if source else None,
        "decided_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if detail:
        payload["detail"] = detail
    try:
        marker_path().parent.mkdir(parents=True, exist_ok=True)
        marker_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("Could not record the legacy-migration marker.", exc_info=True)


def legacy_db_candidates() -> List[Path]:
    """Where a v0.1 ZIP install may have left `data\\jarvis.db`.

    Ordered by confidence. Each is a single existence check; there is no
    globbing and no directory walk. See the module docstring for why this
    is a list rather than a path.
    """
    override = os.environ.get(LEGACY_DB_ENV, "").strip()
    if override:
        # Named explicitly by someone who knows. Trusted enough to look
        # at, still validated like every other candidate below.
        return [Path(override)]

    candidates: List[Path] = []

    def _add(base: Optional[Path], *parts: str) -> None:
        if base is None:
            return
        candidates.append(base.joinpath(*parts))

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        # The v0.2 install directory, in case somebody extracted the old
        # ZIP there before installing over it.
        _add(Path(local_app_data), "Programs", "JARVIS", "data", "jarvis.db")

    # v0.1's QUICKSTART said: "Unzip into a folder, e.g. C:\JARVIS\".
    if _is_windows():
        system_drive = os.environ.get("SystemDrive", "C:")
        _add(Path(f"{system_drive}\\"), "JARVIS", "JARVIS", "data", "jarvis.db")
        _add(Path(f"{system_drive}\\"), "JARVIS", "data", "jarvis.db")

    # Where a downloaded ZIP is extracted if nobody chooses anywhere.
    try:
        home = Path.home()
    except Exception:  # noqa: BLE001 — a profile-less environment is not an error here
        home = None
    for folder in ("Downloads", "Desktop", "Documents"):
        _add(home, folder, "JARVIS", "data", "jarvis.db")

    return candidates


def find_legacy_db() -> Optional[Path]:
    """The first candidate that exists, or None. Existence only — whether
    it is usable is validate_legacy_db()'s question."""
    for candidate in legacy_db_candidates():
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            # A path that cannot even be stat-ed (a disconnected drive, a
            # permission wall) is simply not a candidate.
            continue
    return None


def _open_read_only(path: Path) -> sqlite3.Connection:
    """Open without any possibility of writing to the file.

    `as_uri()` handles spaces and non-ASCII usernames by percent-encoding
    them, which a hand-built `file:` string does not.
    """
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def validate_legacy_db(path: Path) -> bool:
    """Whether this file is a sound JARVIS database.

    Two questions, not one. `integrity_check` says the file is not
    corrupt; the table check says it is *ours*. Without the second, any
    stray SQLite file in one of the candidate locations could be adopted
    as somebody's history.
    """
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
    except OSError:
        return False

    try:
        conn = _open_read_only(path)
    except Exception:  # noqa: BLE001
        return False

    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            return False
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return all(table in names for table in _REQUIRED_LEGACY_TABLES)
    except Exception:  # noqa: BLE001 — not a database, or not readable
        return False
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def destination_has_user_data(path: Path) -> bool:
    """Whether the current database holds anything worth protecting.

    An existing file is not the question — `create_tables()` makes one on
    first launch. What matters is whether there are rows in it. A table
    that does not exist yet contributes nothing rather than raising.
    """
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
    except OSError:
        return False

    try:
        conn = _open_read_only(path)
    except Exception:  # noqa: BLE001
        # Unreadable is not the same as empty. Treated as "has data" so
        # an unreadable destination is never overwritten.
        return True

    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in _USER_DATA_TABLES:
            if table not in names:
                continue
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608 — fixed literals
            if count and count[0]:
                return True
        return False
    except Exception:  # noqa: BLE001
        return True
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _backup(legacy: Path) -> Optional[Path]:
    """Copy the legacy file somewhere safe before anything else happens.
    Best-effort: a backup that cannot be written must not stop a
    migration whose source is never modified anyway."""
    try:
        backups = app_data_root() / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        target = backups / f"legacy_jarvis_{time.strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(legacy, target)
        return target
    except OSError:
        logger.warning("Could not back up the legacy database; continuing.", exc_info=True)
        return None


def _atomic_copy(source: Path, destination: Path) -> None:
    """Copy, then rename into place. An interruption leaves the temporary
    file behind, never a half-written database."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + ".migrating")
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, destination)  # atomic within one directory
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def migrate_if_needed(force: bool = False) -> dict:
    """Decide once whether to carry a v0.1 database forward, and do it.

    Call before anything creates the destination database. **Never
    raises** — every failure path returns a status and lets JARVIS start.

    That guarantee is enforced here rather than assumed of the helpers
    below. Each of them is written to be total, but this runs on the
    startup path of a windowed build with no console, where an unhandled
    exception becomes a modal dialog nobody can dismiss (the same failure
    mode as app/launcher/safe_output.py). Optional data is not worth that.

    `force` skips only the marker check, for tests. It does not skip any
    of the safety checks.
    """
    try:
        return _migrate(force)
    except BaseException as exc:  # noqa: BLE001 — startup must never fail over optional data
        logger.error("Legacy migration failed unexpectedly: %r", exc)
        _write_marker("failed_unexpectedly", detail=exc.__class__.__name__)
        return {"status": "failed_unexpectedly", "error": exc.__class__.__name__}


def _migrate(force: bool) -> dict:
    if not is_frozen():
        # Dev and CI run against the repository, where "the legacy
        # database" would be the developer's own working database.
        return {"status": "not_applicable"}

    if not force:
        marker = _read_marker()
        if marker is not None:
            return {"status": "already_decided", "previous": marker}

    legacy = find_legacy_db()
    if legacy is None:
        _write_marker("no_legacy_found")
        return {"status": "no_legacy_found"}

    destination = default_db_path()

    if destination_has_user_data(destination):
        logger.info(
            "A v0.1 database was found, but this installation already has data — "
            "leaving both alone. Neither file was changed."
        )
        _write_marker("skipped_destination_has_data", source=legacy)
        return {"status": "skipped_destination_has_data", "source": str(legacy)}

    if not validate_legacy_db(legacy):
        logger.warning(
            "A file was found where a v0.1 database would be, but it is not a "
            "readable JARVIS database. It has been left exactly as it is, and "
            "JARVIS is starting with an empty database."
        )
        _write_marker("skipped_invalid_legacy", source=legacy)
        return {"status": "skipped_invalid_legacy", "source": str(legacy)}

    backup = _backup(legacy)

    try:
        _atomic_copy(legacy, destination)
    except OSError as exc:
        logger.error("Could not copy the v0.1 database into place: %s", exc.__class__.__name__)
        _write_marker("failed_copy", source=legacy, detail=exc.__class__.__name__)
        return {"status": "failed_copy", "source": str(legacy)}

    if not validate_legacy_db(destination):
        # The copy arrived damaged. Remove it rather than start on it —
        # an empty database is a better outcome than a corrupt one.
        try:
            destination.unlink()
        except OSError:
            pass
        logger.error("The copied v0.1 database did not survive verification; it was discarded.")
        _write_marker("failed_verification", source=legacy)
        return {"status": "failed_verification", "source": str(legacy)}

    try:
        from db.migrations import create_tables

        create_tables(db_path=destination)
    except Exception as exc:  # noqa: BLE001 — a schema failure must not stop startup
        logger.error("Could not apply the current schema to the imported database: %r", exc)
        _write_marker("failed_schema", source=legacy, detail=exc.__class__.__name__)
        return {"status": "failed_schema", "source": str(legacy)}

    logger.info(
        "Imported a v0.1 database into %s. The original was left untouched.", data_dir()
    )
    _write_marker("migrated", source=legacy)
    return {
        "status": "migrated",
        "source": str(legacy),
        "destination": str(destination),
        "backup": str(backup) if backup else None,
    }


def last_result() -> Optional[dict]:
    """What migrate_if_needed() last recorded, for Diagnostics. None if it
    has never run."""
    return _read_marker()
