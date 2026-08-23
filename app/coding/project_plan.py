"""What creating a new project would do, worked out before anything is done.

Create used to write the project the moment it was pressed. That is one
button between a person and a new folder tree on their disk, with nothing
in between telling them where it would go, what would be in it, or
whether a Git repository would appear. If the destination already
existed, they found out from an error afterwards.

So creation is two steps now, and the first one **touches nothing**.
`build_plan()` reads the filesystem and reports; there is no `mkdir`, no
`open(..., "w")`, no `git` invocation anywhere in this module, and a test
walks its AST to keep it that way.

**A plan is bound to the state it was made in.** It carries the canonical
destination, the template, the name, and a fingerprint of what was at
that destination when the plan was produced. `check_still_valid()`
re-reads and refuses if any of it moved. A plan approved against an empty
folder must not create into a folder that filled up while the dialog was
open — and the check is a re-read, not a remembered boolean, because the
whole point is that the world may have changed.

**Nothing is overwritten and nothing is merged.** An existing destination
is a refusal, whether it is empty or not: "empty" is a race, and a
non-empty folder is somebody's work.

**Bundled templates only, and always offline.** The plan says
`network_use: none` because there is nothing in `templates.py` that could
make a request. Dependencies are listed and deliberately not installed;
that stays a separate, approved action.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from app.coding.workspace import WorkspaceViolation, canonical_root, protected_summary
from app.logging_config import get_logger

logger = get_logger("coding.project_plan")

#: Long enough to read the plan and decide; short enough that a plan left
#: on screen is not still creatable an hour later against a filesystem
#: that has moved on.
PLAN_TTL_SECONDS = 600.0
MAX_PLANS_KEPT = 20


class PlanError(Exception):
    """Carries a message that is already safe to show.

    `reason`, like `WorkspaceViolation.reason`, so a route never calls
    `str()` on an exception — see `folder_requests.FolderRequestError`.
    """

    @property
    def reason(self) -> str:
        return str(self)


@dataclass
class CreationPlan:
    """Exactly what pressing Create would do."""

    id: str
    created_at: float

    parent_path: str
    project_name: str
    template_key: str
    template_title: str
    stack: str
    destination: str

    files: List[str] = field(default_factory=list)
    git_init: bool = False
    initial_branch: str = ""
    dependencies: List[str] = field(default_factory=list)
    installs_dependencies: bool = False
    commands: List[str] = field(default_factory=list)
    network_use: str = "none"
    approximate_bytes: int = 0
    protected_not_created: List[str] = field(default_factory=list)
    validation: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)

    #: What was at the destination when this plan was made. Compared
    #: again at creation time.
    fingerprint: str = ""
    consumed: bool = False

    def expired(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) - self.created_at > PLAN_TTL_SECONDS

    @property
    def creatable(self) -> bool:
        return not self.conflicts and not self.consumed and not self.expired()

    def as_dict(self) -> dict:
        return {
            "plan_id": self.id,
            "created_at": self.created_at,
            "expires_in_seconds": max(
                0, int(PLAN_TTL_SECONDS - (time.time() - self.created_at))),
            "parent_path": self.parent_path,
            "project_name": self.project_name,
            "template": self.template_key,
            "template_title": self.template_title,
            "stack": self.stack,
            "destination": self.destination,
            "files": self.files,
            "file_count": len(self.files),
            "git_init": self.git_init,
            "initial_branch": self.initial_branch,
            "dependencies": self.dependencies,
            "installs_dependencies": self.installs_dependencies,
            "commands": self.commands,
            "network_use": self.network_use,
            "approximate_bytes": self.approximate_bytes,
            "protected_not_created": self.protected_not_created,
            "validation": self.validation,
            "conflicts": self.conflicts,
            "creatable": self.creatable,
            "consumed": self.consumed,
        }


# --------------------------------------------------------------------------
# Building a plan — reads only
# --------------------------------------------------------------------------

def build_plan(parent_path: str, name: str, template_key: str) -> CreationPlan:
    """Work out what creating this project would do. Writes nothing."""
    from app.coding import templates

    clean_name = templates.validate_name(name)
    template = templates._templates().get(template_key)
    if template is None:
        raise PlanError(f"'{template_key}' is not a template JARVIS ships.")

    try:
        parent = canonical_root(parent_path)
    except WorkspaceViolation as exc:
        raise PlanError(str(exc)) from None

    destination = parent / clean_name
    # Same containment check `templates.create` performs, run here so the
    # plan can refuse a name that escapes rather than showing a
    # destination that creation would then reject.
    from app.coding.workspace import resolve

    try:
        contained = resolve(parent, clean_name)
    except WorkspaceViolation as exc:
        raise PlanError(str(exc)) from None
    if contained.absolute != destination:
        raise PlanError("That project name does not resolve inside the folder you chose.")

    rendered = templates._render(template, clean_name)
    files = sorted(rendered)
    approximate = sum(len(content.encode("utf-8")) for content in rendered.values())

    conflicts = _conflicts(parent, destination, clean_name)
    protected = protected_summary()

    plan = CreationPlan(
        id=secrets.token_urlsafe(12),
        created_at=time.time(),
        parent_path=str(parent),
        project_name=clean_name,
        template_key=template.key,
        template_title=template.title,
        stack=template.stack,
        destination=str(destination),
        files=files,
        git_init=template.init_git,
        # `git init -q` with no `-b`: the branch is whatever this machine's
        # Git is configured to make. Naming one here would be a guess, and
        # a plan that guesses is worse than one that says it does not know.
        initial_branch=_configured_branch() if template.init_git else "",
        dependencies=_dependencies(rendered),
        installs_dependencies=False,
        commands=(["git init -q"] if template.init_git else []),
        network_use="none",
        approximate_bytes=approximate,
        protected_not_created=[
            f"Nothing matching {example}" for example in protected.get("pattern_examples", [])
        ][:3] + [
            "No .env file, and no credentials of any kind, are created."
        ],
        validation=_validation(template),
        conflicts=conflicts,
        fingerprint=_fingerprint(destination),
    )
    _store.remember(plan)
    logger.info("Creation plan %s prepared (%s, %d files, %d conflict(s)).",
                plan.id[:8], template.key, len(files), len(conflicts))
    return plan


def _conflicts(parent: Path, destination: Path, name: str) -> List[str]:
    """Everything that would stop this creation, found before it starts."""
    problems: List[str] = []
    try:
        if destination.exists():
            if destination.is_dir():
                try:
                    entries = list(destination.iterdir())
                except OSError:
                    entries = []
                problems.append(
                    f"'{name}' already exists in that folder"
                    + (f" and contains {len(entries)} item(s)" if entries else " (it is empty)")
                    + ". JARVIS will not write into a folder that is already there."
                )
            else:
                problems.append(f"'{name}' already exists in that folder as a file.")
    except OSError as exc:
        problems.append(f"That destination could not be read ({type(exc).__name__}).")

    try:
        # A read-only parent is a failure that would otherwise appear
        # halfway through writing files.
        if not os.access(str(parent), os.W_OK):
            problems.append("JARVIS cannot write into that folder.")
    except OSError:
        pass
    return problems


def _dependencies(files: Dict[str, str]) -> List[str]:
    """What the template declares, read out of what it would write.

    Read from the rendered content rather than a hand-kept list, so a
    template that gains a dependency cannot have a plan that omits it.
    """
    import json
    import re

    found: List[str] = []
    package_json = files.get("package.json", "")
    if package_json:
        try:
            data = json.loads(package_json)
        except ValueError:
            data = {}
        for section in ("dependencies", "devDependencies"):
            for dep, version in sorted((data.get(section) or {}).items()):
                found.append(f"{dep} {version}")

    requirements = files.get("requirements.txt", "")
    for line in requirements.splitlines():
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("#"):
            found.append(cleaned)

    pyproject = files.get("pyproject.toml", "")
    for match in re.findall(r'^\s*"([^"]+)",?\s*$', pyproject, re.MULTILINE):
        if match not in found:
            found.append(match)
    return found


def _validation(template) -> List[str]:
    """What JARVIS will check after writing, in words."""
    checks = [
        "Every file is written inside the destination folder and nowhere else.",
        "The folder is registered as a project, and nothing outside it is touched.",
    ]
    if template.init_git:
        checks.append("git init runs with no remote, no commit and no configuration change.")
    if template.needs_install:
        checks.append(
            "Dependencies are listed but not installed. Installing them is a "
            "separate action you approve.")
    return checks


def _configured_branch() -> str:
    """What this machine's Git would name the first branch.

    Read, never set: `init.defaultBranch` is the user's configuration and
    a project scaffolder has no business changing it. Unset is reported as
    unset rather than guessed — "master" and "main" are both wrong
    answers on some machines.
    """
    import subprocess

    from app.coding.runner import build_environment

    try:
        result = subprocess.run(  # noqa: S603 — argv list, shell=False
            ["git", "config", "--get", "init.defaultBranch"],
            capture_output=True, timeout=10, shell=False, text=True,
            env=build_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    name = (result.stdout or "").strip()
    return name or "whatever your Git is configured to use"


def _fingerprint(destination: Path) -> str:
    """A cheap description of what is at the destination right now."""
    try:
        if not destination.exists():
            return "absent"
        if destination.is_dir():
            try:
                return f"dir:{len(list(destination.iterdir()))}"
            except OSError:
                return "dir:unreadable"
        return f"file:{destination.stat().st_size}"
    except OSError:
        return "unknown"


# --------------------------------------------------------------------------
# Holding plans, and spending one
# --------------------------------------------------------------------------

class _PlanStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plans: Dict[str, CreationPlan] = {}

    def remember(self, plan: CreationPlan) -> None:
        with self._lock:
            self._plans[plan.id] = plan
            if len(self._plans) > MAX_PLANS_KEPT:
                oldest = sorted(self._plans.values(), key=lambda p: p.created_at)
                for stale in oldest[:-MAX_PLANS_KEPT]:
                    self._plans.pop(stale.id, None)

    def get(self, plan_id: str) -> Optional[CreationPlan]:
        with self._lock:
            return self._plans.get(plan_id)

    def cancel(self, plan_id: str) -> bool:
        """Forget a plan. Nothing was written, so there is nothing to undo."""
        with self._lock:
            return self._plans.pop(plan_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._plans.clear()


_store = _PlanStore()


def get(plan_id: str) -> Optional[CreationPlan]:
    return _store.get(plan_id)


def cancel(plan_id: str) -> bool:
    return _store.cancel(plan_id)


def clear() -> None:
    _store.clear()


def check_still_valid(plan: CreationPlan) -> None:
    """Refuse a plan the world has moved out from under. Raises or returns.

    Re-reads the destination rather than trusting the conflict list the
    plan carries: the gap between planning and confirming is exactly when
    somebody creates the folder, and a remembered "no conflicts" would
    then be a stale answer to the only question that matters.
    """
    if plan.consumed:
        raise PlanError("That plan has already been used. Make a new one.")
    if plan.expired():
        raise PlanError("That plan has expired. Check the details and confirm again.")

    destination = Path(plan.destination)
    current = _fingerprint(destination)
    if current != plan.fingerprint:
        raise PlanError(
            "The destination folder has changed since this plan was made, so "
            "JARVIS did not create anything. Review the plan again."
        )
    if destination.exists():
        raise PlanError(
            f"'{plan.project_name}' already exists in that folder. JARVIS will "
            "not write into a folder that is already there."
        )
    if plan.conflicts:
        raise PlanError(plan.conflicts[0])


def execute(plan: CreationPlan):
    """Create the project this plan describes.

    The only writing path, and it is reachable only with a plan that has
    just been re-checked against the filesystem.
    """
    from app.coding import templates

    check_still_valid(plan)
    plan.consumed = True
    try:
        return templates.create(plan.parent_path, plan.project_name, plan.template_key)
    except Exception:
        # A failed creation must not leave a plan marked spent: the user
        # should be able to fix the cause and confirm the same plan again.
        plan.consumed = False
        raise
