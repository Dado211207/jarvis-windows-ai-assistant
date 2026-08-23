"""The project registry — the list of folders the user has explicitly
handed to Coding Workspace.

**Coding Workspace is off until this list has an entry.** There is no
default project, no "current directory", no recent-folders scan and no
inference from anything the user typed. A project exists here because
somebody chose a folder in a picker and pressed a button.

**Removing a project removes the entry, never the files.** This is the
kind of thing that must be true in the code and not only in the button
label, so `remove()` touches nothing on disk and a test asserts the files
survive.

The registry is a plain JSON file under `data_dir()`, following the same
pattern as `app/core/preferences.py`: it never raises, and an unreadable
file means "no projects", which degrades to Coding Workspace being off —
the safe direction.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from app.core.app_paths import data_dir
from app.coding.workspace import WorkspaceViolation, canonical_root
from app.logging_config import get_logger

logger = get_logger("coding.projects")

REGISTRY_FILENAME = "coding_projects.json"
MAX_PROJECTS = 50

# A project name is a label, not a path. It is shown in the UI and used in
# nothing that touches the filesystem, but it is still bounded and
# stripped of anything that would let it impersonate a path.
_NAME_SAFE = re.compile(r"[^\w .\-()+]", re.UNICODE)


@dataclass
class Project:
    id: str
    name: str
    root: str
    added_at: float
    last_opened_at: Optional[float] = None
    trusted: bool = False
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _registry_path() -> Optional[Path]:
    try:
        return data_dir() / REGISTRY_FILENAME
    except Exception:  # noqa: BLE001 — a missing AppData must not take the app down
        logger.warning("Could not determine the data directory.", exc_info=True)
        return None


def _load_raw() -> List[dict]:
    path = _registry_path()
    if path is None or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("The coding project registry could not be read; treating it as empty.")
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _save(projects: List[Project]) -> bool:
    path = _registry_path()
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([p.as_dict() for p in projects], indent=2)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(payload, encoding="utf-8")
        temp.replace(path)
        return True
    except OSError:
        logger.warning("Could not write the coding project registry.", exc_info=True)
        return False


def _coerce(entry: dict) -> Optional[Project]:
    try:
        return Project(
            id=str(entry["id"]),
            name=str(entry.get("name") or "Project"),
            root=str(entry["root"]),
            added_at=float(entry.get("added_at") or 0.0),
            last_opened_at=(float(entry["last_opened_at"]) if entry.get("last_opened_at") else None),
            trusted=bool(entry.get("trusted", False)),
            notes=[str(n) for n in entry.get("notes", []) if isinstance(n, str)],
        )
    except (KeyError, TypeError, ValueError):
        return None


def list_projects() -> List[Project]:
    projects = [p for p in (_coerce(e) for e in _load_raw()) if p is not None]
    projects.sort(key=lambda p: (p.last_opened_at or p.added_at), reverse=True)
    return projects


def get(project_id: str) -> Optional[Project]:
    for project in list_projects():
        if project.id == project_id:
            return project
    return None


def safe_name(raw: str, fallback: str = "Project") -> str:
    cleaned = _NAME_SAFE.sub("", (raw or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:60] or fallback


def add(root_path: str, name: str = "") -> Project:
    """Register a folder the user selected. Raises WorkspaceViolation if
    the folder is not a usable, safe project root."""
    canonical = canonical_root(root_path)

    existing = list_projects()
    for project in existing:
        try:
            if Path(project.root).resolve(strict=False) == canonical:
                # Already registered. Return the existing entry rather than
                # creating a duplicate that would confuse every later lookup.
                return project
        except OSError:
            continue

    if len(existing) >= MAX_PROJECTS:
        raise WorkspaceViolation(
            f"The project list is full ({MAX_PROJECTS}). Remove one before adding another."
        )

    project = Project(
        id=uuid.uuid4().hex[:12],
        name=safe_name(name or canonical.name, fallback=canonical.name or "Project"),
        root=str(canonical),
        added_at=time.time(),
        trusted=True,  # the user chose this folder in a picker; that IS the trust decision
    )
    existing.append(project)
    _save(existing)
    logger.info("Coding project registered (%s).", project.id)
    return project


def remove(project_id: str) -> bool:
    """Forget a project. **Never touches the folder or any file in it.**

    Deliberately has no filesystem call of any kind in its body — the
    guarantee is structural, not a promise, and
    `test_removing_a_project_never_deletes_files` asserts the files are
    still there afterwards.
    """
    projects = list_projects()
    remaining = [p for p in projects if p.id != project_id]
    if len(remaining) == len(projects):
        return False
    _save(remaining)
    logger.info("Coding project removed from the list (%s); files untouched.", project_id)
    return True


def touch_opened(project_id: str) -> Optional[Project]:
    projects = list_projects()
    found = None
    for project in projects:
        if project.id == project_id:
            project.last_opened_at = time.time()
            found = project
            break
    if found is not None:
        _save(projects)
    return found


def resolve_root(project_id: str) -> Path:
    """The live canonical root for a registered project.

    Re-canonicalized on every call: a project folder that has been deleted,
    moved or replaced by a link since it was registered is not the folder
    the user chose, and must not be treated as it.
    """
    project = get(project_id)
    if project is None:
        raise WorkspaceViolation("That project is not in the list.")
    try:
        return canonical_root(project.root)
    except WorkspaceViolation:
        raise WorkspaceViolation(
            f"'{project.name}' is no longer available at the folder it was added from."
        ) from None


def is_enabled() -> bool:
    """Coding Workspace is enabled only once a project exists.

    The UI uses this to stay in an explicit empty state rather than
    presenting a coding agent to somebody who never asked for one.
    """
    return bool(list_projects())
