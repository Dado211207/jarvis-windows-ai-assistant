"""Task records — what happened, in a form that is safe to keep.

**What is stored:** the request, the approved plan, the project id,
project-relative paths, before/after hashes, redacted commands, exit
codes, test summaries, approvals, and the outcome.

**What is never stored:** file contents, secrets, `.env` values,
credentials, full command environments, request bodies, audio, clipboard.

The distinction matters because a task record is a file on the user's
disk that outlives the task. "We only log what we need" is a claim; a
module whose write path cannot accept a file body is a mechanism.
`append_step()` takes typed fields, not a free-form blob, so there is no
parameter through which a file's contents could arrive.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.app_paths import data_dir
from app.core.redaction import redact_message
from app.logging_config import get_logger

logger = get_logger("coding.tasks")

TASKS_FILENAME = "coding_tasks.json"
MAX_TASKS_KEPT = 100
MAX_STEPS_KEPT = 400
MAX_STEP_TEXT = 4000


class TaskState(str, Enum):
    PLANNING = "planning"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    STOPPED = "stopped"
    FAILED = "failed"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


TERMINAL_STATES = {TaskState.STOPPED, TaskState.FAILED, TaskState.COMPLETED}


@dataclass
class TaskStep:
    at: float
    kind: str            # inspect | search | patch | command | preview | browser | approval | note | error
    summary: str
    detail: Dict[str, Any] = field(default_factory=dict)
    ok: Optional[bool] = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class TaskRecord:
    id: str
    project_id: str
    request: str
    created_at: float
    state: str = TaskState.PLANNING.value
    updated_at: float = 0.0
    plan: Dict[str, Any] = field(default_factory=dict)
    steps: List[TaskStep] = field(default_factory=list)
    files_changed: List[Dict[str, Any]] = field(default_factory=list)
    pre_existing_changes: Dict[str, Any] = field(default_factory=dict)
    isolation: Dict[str, Any] = field(default_factory=dict)
    provider: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["steps"] = [s.as_dict() if isinstance(s, TaskStep) else s for s in self.steps]
        return data


def _tasks_path() -> Optional[Path]:
    try:
        return data_dir() / TASKS_FILENAME
    except Exception:  # noqa: BLE001
        return None


def _load_raw() -> List[dict]:
    path = _tasks_path()
    if path is None or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("The coding task history could not be read; treating it as empty.")
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _save_raw(entries: List[dict]) -> bool:
    path = _tasks_path()
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(entries[-MAX_TASKS_KEPT:], indent=2), encoding="utf-8")
        temp.replace(path)
        return True
    except OSError:
        logger.warning("Could not write the coding task history.", exc_info=True)
        return False


def _coerce(entry: dict) -> Optional[TaskRecord]:
    try:
        record = TaskRecord(
            id=str(entry["id"]),
            project_id=str(entry.get("project_id", "")),
            request=str(entry.get("request", "")),
            created_at=float(entry.get("created_at") or 0.0),
            state=str(entry.get("state") or TaskState.PLANNING.value),
            updated_at=float(entry.get("updated_at") or 0.0),
            plan=dict(entry.get("plan") or {}),
            files_changed=list(entry.get("files_changed") or []),
            pre_existing_changes=dict(entry.get("pre_existing_changes") or {}),
            isolation=dict(entry.get("isolation") or {}),
            provider=dict(entry.get("provider") or {}),
            result=dict(entry.get("result") or {}),
        )
        record.steps = [
            TaskStep(
                at=float(s.get("at") or 0.0),
                kind=str(s.get("kind") or "note"),
                summary=str(s.get("summary") or ""),
                detail=dict(s.get("detail") or {}),
                ok=s.get("ok"),
            )
            for s in (entry.get("steps") or [])
            if isinstance(s, dict)
        ]
        return record
    except (KeyError, TypeError, ValueError):
        return None


def list_tasks(project_id: str = "") -> List[TaskRecord]:
    records = [r for r in (_coerce(e) for e in _load_raw()) if r is not None]
    if project_id:
        records = [r for r in records if r.project_id == project_id]
    records.sort(key=lambda r: r.created_at, reverse=True)
    return records


def get(task_id: str) -> Optional[TaskRecord]:
    for record in list_tasks():
        if record.id == task_id:
            return record
    return None


def create(project_id: str, request: str) -> TaskRecord:
    record = TaskRecord(
        id=uuid.uuid4().hex[:16],
        project_id=project_id,
        # The request is the user's own words. Redacted anyway: somebody
        # may paste a key into a task description, and a task record is a
        # file that outlives the moment they realise it.
        request=redact_message(request.strip())[:2000],
        created_at=time.time(),
        updated_at=time.time(),
    )
    entries = _load_raw()
    entries.append(record.as_dict())
    _save_raw(entries)
    return record


def save(record: TaskRecord) -> None:
    record.updated_at = time.time()
    record.steps = record.steps[-MAX_STEPS_KEPT:]
    entries = _load_raw()
    for index, entry in enumerate(entries):
        if entry.get("id") == record.id:
            entries[index] = record.as_dict()
            break
    else:
        entries.append(record.as_dict())
    _save_raw(entries)


def append_step(
    record: TaskRecord,
    kind: str,
    summary: str,
    detail: Optional[Dict[str, Any]] = None,
    ok: Optional[bool] = None,
) -> TaskStep:
    """Add one activity entry.

    Both the summary and every string in the detail go through
    `redact_message`. There is no `content` parameter and no way to pass
    a file body: the typed signature is the mechanism, not the intention.
    """
    safe_detail: Dict[str, Any] = {}
    for key, value in (detail or {}).items():
        if isinstance(value, str):
            safe_detail[key] = redact_message(value)[:MAX_STEP_TEXT]
        elif isinstance(value, (int, float, bool)) or value is None:
            safe_detail[key] = value
        elif isinstance(value, list):
            safe_detail[key] = [
                redact_message(v)[:400] if isinstance(v, str) else v
                for v in value[:60]
            ]
        elif isinstance(value, dict):
            safe_detail[key] = {
                str(k): (redact_message(v)[:400] if isinstance(v, str) else v)
                for k, v in list(value.items())[:40]
            }
    step = TaskStep(
        at=time.time(),
        kind=kind,
        summary=redact_message(summary)[:MAX_STEP_TEXT],
        detail=safe_detail,
        ok=ok,
    )
    record.steps.append(step)
    return step


def set_state(record: TaskRecord, state: TaskState, result: Optional[Dict[str, Any]] = None) -> None:
    record.state = state.value
    if result is not None:
        record.result = result
    save(record)


def delete(task_id: str) -> bool:
    entries = _load_raw()
    remaining = [e for e in entries if e.get("id") != task_id]
    if len(remaining) == len(entries):
        return False
    _save_raw(remaining)
    return True


def clear(project_id: str = "") -> int:
    entries = _load_raw()
    if project_id:
        remaining = [e for e in entries if e.get("project_id") != project_id]
    else:
        remaining = []
    removed = len(entries) - len(remaining)
    _save_raw(remaining)
    return removed


def interrupted_tasks() -> List[TaskRecord]:
    """Tasks that were running when the process stopped.

    A task record whose state is RUNNING at startup cannot be running —
    nothing survived the restart. It is reported as INTERRUPTED so the
    user is offered inspect / archive / undo, and **never resumed
    automatically**: re-running a command whose outcome nobody observed is
    how a half-finished install becomes two.
    """
    live = {TaskState.RUNNING.value, TaskState.AWAITING_APPROVAL.value,
            TaskState.AWAITING_PLAN_APPROVAL.value, TaskState.PLANNING.value}
    return [r for r in list_tasks() if r.state in live]


def mark_interrupted_on_startup() -> int:
    """Called once at startup. Returns how many were reclassified."""
    entries = _load_raw()
    changed = 0
    live = {TaskState.RUNNING.value, TaskState.AWAITING_APPROVAL.value,
            TaskState.AWAITING_PLAN_APPROVAL.value, TaskState.PLANNING.value}
    for entry in entries:
        if entry.get("state") in live:
            entry["state"] = TaskState.INTERRUPTED.value
            entry["updated_at"] = time.time()
            changed += 1
    if changed:
        _save_raw(entries)
        logger.info("%d interrupted coding task(s) found at startup.", changed)
    return changed


def redacted_report(record: TaskRecord) -> dict:
    """An export the user can share: paths and outcomes, no content."""
    return {
        "task_id": record.id,
        "request": record.request,
        "state": record.state,
        "created_at": record.created_at,
        "plan": record.plan,
        "isolation": record.isolation,
        "provider": {k: v for k, v in record.provider.items() if k != "api_key"},
        "pre_existing_changes": record.pre_existing_changes,
        "files_changed": [
            {
                "path": f.get("path"),
                "kind": f.get("kind"),
                "lines_added": f.get("lines_added"),
                "lines_removed": f.get("lines_removed"),
                "before_sha256": f.get("before_sha256"),
                "after_sha256": f.get("after_sha256"),
            }
            for f in record.files_changed
        ],
        "steps": [
            {"at": s.at, "kind": s.kind, "summary": s.summary, "ok": s.ok}
            for s in record.steps
        ],
        "result": record.result,
    }
