"""Durable, fail-closed backups for Coding Workspace undo.

Backups live under JARVIS's private data directory, never in the project
and never in task JSON served by the API.  A task record carries only an
opaque backup id plus hashes.  Undo revalidates the current file against
what JARVIS last wrote before restoring exact original bytes.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import threading
from pathlib import Path
from typing import Dict, List, Optional

from app.coding import editing
from app.coding.workspace import WorkspaceViolation, resolve
from app.core.app_paths import data_dir

_UNDO_DIRNAME = "coding_undo"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_UNDO_TASK_BYTES = 64 * 1024 * 1024
MAX_UNDO_TOTAL_BYTES = 512 * 1024 * 1024
_store_lock = threading.Lock()


class UndoBackupError(Exception):
    """A backup could not be created or trusted."""


def _undo_root() -> Path:
    return data_dir() / _UNDO_DIRNAME


def _safe_component(value: str, label: str) -> str:
    text = str(value or "")
    if not _SAFE_ID.fullmatch(text):
        raise UndoBackupError(f"The {label} is not valid.")
    return text


def _backup_path(task_id: str, backup_id: str) -> Path:
    task = _safe_component(task_id, "task id")
    backup = _safe_component(backup_id, "backup id")
    return _undo_root() / task / f"{backup}.bin"


def _directory_size(path: Path) -> int:
    total = 0
    try:
        for candidate in path.rglob("*.bin"):
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def store_bytes(task_id: str, raw: bytes) -> str:
    """Persist exact pre-edit bytes before an edit is allowed to run.

    Normal uninstall preserves this directory with other JARVIS data;
    full removal deletes the data directory. Task delete, clear and
    history pruning purge backups whose undo records no longer exist.
    """
    backup_id = secrets.token_hex(16)
    path = _backup_path(task_id, backup_id)
    temp = path.with_suffix(".tmp")
    try:
        with _store_lock:
            task_bytes = _directory_size(path.parent)
            total_bytes = _directory_size(_undo_root())
            if task_bytes + len(raw) > MAX_UNDO_TASK_BYTES:
                raise UndoBackupError(
                    "This task reached the undo-backup size limit, so the file was not changed."
                )
            if total_bytes + len(raw) > MAX_UNDO_TOTAL_BYTES:
                raise UndoBackupError(
                    "JARVIS reached the total undo-backup size limit. Clear old task "
                    "history before changing more files."
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp, "xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        return backup_id
    except UndoBackupError:
        raise
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise UndoBackupError(
            f"JARVIS could not create the undo backup ({type(exc).__name__}), "
            "so the file was not changed."
        ) from None


def purge_task(task_id: str) -> bool:
    """Remove every backup owned by one deleted or pruned task."""
    try:
        task = _safe_component(task_id, "task id")
        target = _undo_root() / task
        if not target.exists():
            return True
        shutil.rmtree(target)
        return True
    except (OSError, UndoBackupError):
        return False


def purge_except(task_ids: List[str]) -> int:
    """Remove backup directories not represented in retained history."""
    keep = {
        str(task_id) for task_id in task_ids
        if _SAFE_ID.fullmatch(str(task_id or ""))
    }
    root = _undo_root()
    try:
        candidates = list(root.iterdir()) if root.is_dir() else []
    except OSError:
        return 0
    removed = 0
    for candidate in candidates:
        if (
            candidate.is_dir()
            and _SAFE_ID.fullmatch(candidate.name)
            and candidate.name not in keep
        ):
            try:
                shutil.rmtree(candidate)
                removed += 1
            except OSError:
                continue
    return removed

def discard(task_id: str, backup_id: Optional[str]) -> None:
    if not backup_id:
        return
    try:
        _backup_path(task_id, backup_id).unlink(missing_ok=True)
    except (OSError, UndoBackupError):
        pass


def _load(task_id: str, backup_id: str, expected_sha256: str) -> bytes:
    path = _backup_path(task_id, backup_id)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UndoBackupError(
            f"The undo backup is unavailable ({type(exc).__name__}); nothing was changed."
        ) from None
    if editing.sha256_bytes(raw) != expected_sha256:
        raise UndoBackupError("The undo backup failed its integrity check; nothing was changed.")
    return raw


def _current_snapshot(root: Path, relative: str):
    target = resolve(root, relative, must_exist=True)
    return target, editing.read_snapshot(target)


def _skip(change: dict, reason: str) -> dict:
    return {
        "path": str(change.get("path") or ""),
        "destination": str(change.get("destination") or ""),
        "kind": str(change.get("kind") or ""),
        "reverted": False,
        "reason": reason,
    }


def _done(change: dict) -> dict:
    return {
        "path": str(change.get("path") or ""),
        "destination": str(change.get("destination") or ""),
        "kind": str(change.get("kind") or ""),
        "reverted": True,
        "reason": "",
    }


def _undo_one(root: Path, task_id: str, change: Dict[str, object]) -> dict:
    kind = str(change.get("kind") or "")
    path = str(change.get("path") or "")
    destination = str(change.get("destination") or "")
    before_sha = str(change.get("before_sha256") or "")
    after_sha = str(change.get("after_sha256") or "")
    backup_id = str(change.get("undo_backup") or "")

    if kind == "create":
        target, snapshot = _current_snapshot(root, path)
        if not after_sha or snapshot.sha256 != after_sha:
            return _skip(change, "changed after JARVIS created it; left alone")
        target.absolute.unlink()
        return _done(change)

    if kind == "update":
        target, snapshot = _current_snapshot(root, path)
        if not after_sha or snapshot.sha256 != after_sha:
            return _skip(change, "changed after JARVIS edited it; left alone")
        raw = _load(task_id, backup_id, before_sha)
        editing.atomic_write_bytes(target, raw)
        discard(task_id, backup_id)
        return _done(change)

    if kind == "delete":
        target = resolve(root, path)
        if target.absolute.exists():
            return _skip(change, "a file now exists at the deleted path; left alone")
        raw = _load(task_id, backup_id, before_sha)
        editing.atomic_write_bytes(target, raw)
        discard(task_id, backup_id)
        return _done(change)

    if kind == "rename":
        if not destination:
            return _skip(change, "the recorded rename has no destination")
        source = resolve(root, path)
        if source.absolute.exists():
            return _skip(change, "a file now exists at the original path; left alone")
        target, snapshot = _current_snapshot(root, destination)
        if not after_sha or snapshot.sha256 != after_sha:
            return _skip(change, "the renamed file changed after JARVIS moved it; left alone")
        # Verify the durable original before moving anything.  A rename
        # does not alter bytes, so the backup and destination must agree.
        raw = _load(task_id, backup_id, before_sha)
        if editing.sha256_bytes(raw) != snapshot.sha256:
            return _skip(change, "the renamed file no longer matches its undo backup")
        source.absolute.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target.absolute, source.absolute)
        discard(task_id, backup_id)
        return _done(change)

    return _skip(change, "this task record does not describe an undoable change")


def restore_task_changes(root: Path, task_id: str, changes: List[dict]) -> List[dict]:
    """Undo recorded changes in reverse order, refusing every stale path."""
    outcomes: List[dict] = []
    for change in reversed(list(changes)):
        try:
            outcomes.append(_undo_one(root, task_id, change))
        except (OSError, WorkspaceViolation, UndoBackupError) as exc:
            reason = exc.reason if isinstance(exc, WorkspaceViolation) else str(exc)
            outcomes.append(_skip(change, reason))
        except Exception as exc:  # noqa: BLE001 - one bad record must not touch another
            outcomes.append(_skip(change, f"undo failed safely ({type(exc).__name__})"))
    return outcomes
