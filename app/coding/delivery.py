"""Review and explicit export delivery for isolated coding tasks.

Task worktrees are never silently merged into the user's working copy.
Instead JARVIS renders the exact task-root diff, then exports an approved,
content-addressed ZIP containing the patch, final changed files and a
manifest.  The plan is short-lived and is invalidated by any later edit.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from app.coding import editing, gitsafe
from app.coding.workspace import WorkspaceViolation, is_protected, resolve
from app.core.app_paths import data_dir

MAX_REVIEW_DIFF_BYTES = 500_000
MAX_EXPORT_BYTES = 32 * 1024 * 1024
PLAN_TTL_SECONDS = 300.0
_SAFE_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_lock = threading.Lock()
_plans: Dict[str, "ExportPlan"] = {}


@dataclass
class ExportPlan:
    id: str
    task_id: str
    start_sha: str
    fingerprint: str
    paths: List[str]
    total_bytes: int
    expires_at: float

    def as_dict(self) -> dict:
        return {
            "plan_id": self.id,
            "task_id": self.task_id,
            "paths": list(self.paths),
            "file_count": len(self.paths),
            "total_bytes": self.total_bytes,
            "expires_in_seconds": max(0, int(self.expires_at - time.time())),
            "delivery": "downloadable ZIP; the user project is not modified",
        }


def _safe_id(value: str) -> str:
    text = str(value or "")
    if not text or len(text) > 64 or any(ch not in _SAFE_ID_CHARS for ch in text):
        raise WorkspaceViolation("The export id is not valid.")
    return text


def task_paths(record) -> List[str]:
    paths = set()
    for change in record.files_changed:
        path = str(change.get("path") or "")
        destination = str(change.get("destination") or "")
        if path:
            paths.add(path)
        if destination:
            paths.add(destination)
    return sorted(paths)


def _snapshot(root: Path, start_sha: str, paths: List[str]) -> tuple[str, int]:
    rows = []
    total = 0
    for relative in paths:
        target = resolve(root, relative)
        if is_protected(target.relative) is not None:
            raise WorkspaceViolation(f"Protected path {target.display!r} cannot be exported.")
        if target.absolute.is_file():
            raw = target.absolute.read_bytes()
            total += len(raw)
            rows.append([target.display, hashlib.sha256(raw).hexdigest(), len(raw)])
        elif target.absolute.exists():
            raise WorkspaceViolation(f"{target.display!r} is not a regular file.")
        else:
            rows.append([target.display, "missing", 0])
    payload = json.dumps({"start_sha": start_sha, "paths": rows},
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), total


def review_diff(root: Path, start_sha: str, paths: List[str]) -> str:
    """Return the task-root diff, including newly-created untracked files."""
    if not start_sha or not paths:
        return ""
    permitted = []
    for relative in paths:
        target = resolve(root, relative)
        if is_protected(target.relative) is None:
            permitted.append(target.display)
    if not permitted:
        return ""

    code, out, _ = gitsafe._git(
        root, ["diff", "--no-color", "--binary", start_sha, "--", *permitted],
        timeout=30.0,
    )
    body = out if code == 0 else ""
    code, tracked_text, _ = gitsafe._git(
        root, ["ls-tree", "-r", "--name-only", start_sha, "--", *permitted],
        timeout=30.0,
    )
    tracked = set(tracked_text.splitlines()) if code == 0 else set()
    for relative in permitted:
        if relative in tracked:
            continue
        target = resolve(root, relative)
        if not target.absolute.is_file():
            continue
        raw = target.absolute.read_bytes()
        if editing.looks_binary(raw):
            body += f"\nBinary file created: {relative} ({len(raw)} bytes)\n"
            continue
        snapshot = editing.read_snapshot(target)
        body += "\n" + editing.unified_diff("", snapshot.text, relative)
    encoded = body.encode("utf-8", errors="replace")
    if len(encoded) > MAX_REVIEW_DIFF_BYTES:
        body = encoded[:MAX_REVIEW_DIFF_BYTES].decode("utf-8", errors="ignore")
        body += f"\n[task diff truncated at {MAX_REVIEW_DIFF_BYTES:,} bytes]\n"
    return body


def plan_export(record, root: Path) -> ExportPlan:
    start_sha = str(record.isolation.get("start_sha") or "")
    paths = task_paths(record)
    if not start_sha or not paths:
        raise WorkspaceViolation("This task has no isolated changes to export.")
    fingerprint, total = _snapshot(root, start_sha, paths)
    if total > MAX_EXPORT_BYTES:
        raise WorkspaceViolation(
            f"The task export is {total:,} bytes, above the {MAX_EXPORT_BYTES:,}-byte limit."
        )
    plan = ExportPlan(
        id=secrets.token_urlsafe(18), task_id=record.id, start_sha=start_sha,
        fingerprint=fingerprint, paths=paths, total_bytes=total,
        expires_at=time.time() + PLAN_TTL_SECONDS,
    )
    with _lock:
        now = time.time()
        for expired_id, existing in list(_plans.items()):
            if now > existing.expires_at:
                _plans.pop(expired_id, None)
        while len(_plans) >= 128:
            _plans.pop(next(iter(_plans)))
        _plans[plan.id] = plan
    return plan


def create_export(plan_id: str, record, root: Path) -> tuple[str, Path]:
    with _lock:
        plan = _plans.pop(str(plan_id or ""), None)
    if plan is None or plan.task_id != record.id or time.time() > plan.expires_at:
        raise WorkspaceViolation("That export plan expired. Review the task again.")
    fingerprint, total = _snapshot(root, plan.start_sha, plan.paths)
    if fingerprint != plan.fingerprint or total != plan.total_bytes:
        raise WorkspaceViolation(
            "The task worktree changed after review. Review it again before exporting."
        )

    token = secrets.token_urlsafe(24)
    directory = data_dir() / "coding_exports" / _safe_id(record.id)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{token}.zip"
    temp = destination.with_suffix(".tmp")
    manifest = {
        "task_id": record.id,
        "start_sha": plan.start_sha,
        "fingerprint": plan.fingerprint,
        "paths": plan.paths,
        "note": "Review changes.patch before applying these files to another tree.",
    }
    try:
        with zipfile.ZipFile(temp, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            archive.writestr(
                "changes.patch", review_diff(root, plan.start_sha, plan.paths)
            )
            for relative in plan.paths:
                target = resolve(root, relative)
                if target.absolute.is_file():
                    archive.write(target.absolute, arcname=f"files/{target.display}")
        temp.replace(destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return token, destination


def export_path(task_id: str, token: str) -> Path:
    task = _safe_id(task_id)
    safe_token = _safe_id(token)
    path = data_dir() / "coding_exports" / task / f"{safe_token}.zip"
    if not path.is_file():
        raise WorkspaceViolation("That task export is not available.")
    return path


def purge_task(task_id: str) -> bool:
    try:
        target = data_dir() / "coding_exports" / _safe_id(task_id)
        if target.exists():
            shutil.rmtree(target)
        return True
    except (OSError, WorkspaceViolation):
        return False
