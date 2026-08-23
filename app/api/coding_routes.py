"""Coding Workspace HTTP surface.

Every mutating endpoint carries `Depends(require_session_token)`, which
is not a convention here but an enforced one:
`tests/test_security_invariants.py::test_every_mutating_endpoint_requires_the_session_token`
walks the assembled application and fails the build for any that does
not.

Read endpoints are deliberately readable without a token, matching the
rest of the API — they expose project names, paths relative to the
project, and state, never file contents or secrets.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.session import require_session_token
from app.coding import commands as command_policy
from app.coding import gitsafe, limits, projects, registry, stacks, tasks
from app.coding.runner import ledger
from app.coding.workspace import WorkspaceViolation
from app.core.errors import ErrorCategory, to_safe_error
from app.logging_config import get_logger

logger = get_logger("api.coding")

router = APIRouter(prefix="/coding", tags=["coding"])


# --------------------------------------------------------------------------
# Request models — every mutating body is typed and bounded.
# --------------------------------------------------------------------------

class AddProjectRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4000)
    name: str = Field(default="", max_length=120)


class CreateProjectRequest(BaseModel):
    parent_path: str = Field(min_length=1, max_length=4000)
    name: str = Field(min_length=1, max_length=80)
    template: str = Field(default="static", max_length=40)


class StartTaskRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    request: str = Field(min_length=1, max_length=4000)


class TaskIdRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)


def _safe_failure(exc: Exception, context: str) -> HTTPException:
    """Never return raw exception text — the existing invariant test
    checks for this across every endpoint."""
    error = to_safe_error(exc, category=ErrorCategory.TOOL_ERROR, context=context)
    return HTTPException(
        status_code=400,
        detail=f"{error.message} (reference: {error.correlation_id[:8]})",
    )


# --------------------------------------------------------------------------
# Status and projects
# --------------------------------------------------------------------------

@router.get("/status")
async def coding_status() -> dict:
    """Whether Coding Workspace is usable at all, and why not if it is not.

    `enabled` is False until the user has added a project. The page uses
    this to present an explicit empty state rather than a coding agent
    nobody asked for.
    """
    from app.core.privacy import privacy_mode

    all_projects = projects.list_projects()
    return {
        "enabled": bool(all_projects),
        "project_count": len(all_projects),
        "privacy_mode": privacy_mode.active,
        "privacy_note": (
            "Privacy mode is on, so project content cannot be sent to a cloud model. "
            "A local provider is required while it stays on."
            if privacy_mode.active else ""
        ),
        "live_processes": ledger.live_count(),
        "capabilities": registry.as_matrix(),
        "risk_matrix": command_policy.describe_matrix(),
        "limits": {
            "max_steps": limits.MAX_STEPS,
            "max_commands": limits.MAX_COMMANDS,
            "max_files_edited": limits.MAX_FILES_EDITED,
            "max_elapsed_minutes": int(limits.MAX_ELAPSED_SECONDS // 60),
            "max_preview_minutes": int(limits.MAX_PREVIEW_LIFETIME_SECONDS // 60),
        },
        "disabled_in_this_version": [
            "git push", "pull request creation", "merge", "deployment",
            "remote repository cloning", "general internet browsing",
        ],
    }


def _project_payload(project: projects.Project) -> dict:
    payload = project.as_dict()
    try:
        root = Path(project.root)
        available = root.is_dir()
    except OSError:
        available = False
    payload["available"] = available

    if not available:
        payload["stack"] = {"label": "Folder not available"}
        payload["git"] = {"is_repository": False}
        return payload

    detected = stacks.detect(root)
    payload["stack"] = detected.as_dict()
    payload["commands"] = stacks.project_commands(detected)
    payload["missing_intents"] = stacks.missing_intents(detected)

    state = gitsafe.status(root)
    payload["git"] = state.as_dict()
    payload["has_pre_existing_changes"] = state.is_dirty
    return payload


@router.get("/projects")
async def list_coding_projects() -> dict:
    return {"projects": [_project_payload(p) for p in projects.list_projects()]}


@router.post("/projects", dependencies=[Depends(require_session_token)])
async def add_coding_project(body: AddProjectRequest) -> dict:
    try:
        project = projects.add(body.path, body.name)
    except WorkspaceViolation as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from None
    except Exception as exc:  # noqa: BLE001
        raise _safe_failure(exc, "add coding project") from None
    return {"project": _project_payload(project)}


@router.delete("/projects/{project_id}", dependencies=[Depends(require_session_token)])
async def remove_coding_project(project_id: str) -> dict:
    """Forget a project. The folder and every file in it are untouched."""
    removed = projects.remove(project_id)
    if not removed:
        raise HTTPException(status_code=404, detail="That project is not in the list.")
    return {
        "removed": True,
        "files_deleted": False,
        "message": "Removed from JARVIS's list. No files were deleted.",
    }


@router.post("/projects/{project_id}/open", dependencies=[Depends(require_session_token)])
async def open_coding_project(project_id: str) -> dict:
    project = projects.touch_opened(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="That project is not in the list.")
    return {"project": _project_payload(project)}


@router.post("/projects/create", dependencies=[Depends(require_session_token)])
async def create_coding_project(body: CreateProjectRequest) -> dict:
    """Scaffold a new project from a bundled template.

    The plan is returned by `GET /coding/templates` and shown before this
    is called; nothing here reaches the network, and no dependency is
    installed — that stays a separate, approved action.
    """
    from app.coding import templates

    try:
        project = templates.create(body.parent_path, body.name, body.template)
    except WorkspaceViolation as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from None
    except Exception as exc:  # noqa: BLE001
        raise _safe_failure(exc, "create coding project") from None
    return {"project": _project_payload(project)}


@router.get("/templates")
async def list_templates() -> dict:
    from app.coding import templates
    return {"templates": templates.describe_all()}


# --------------------------------------------------------------------------
# Project inspection
# --------------------------------------------------------------------------

@router.get("/projects/{project_id}/git")
async def project_git(project_id: str) -> dict:
    try:
        root = projects.resolve_root(project_id)
    except WorkspaceViolation as exc:
        raise HTTPException(status_code=404, detail=exc.reason) from None
    state = gitsafe.status(root)
    return {
        "status": state.as_dict(),
        "remotes": gitsafe.remotes(root),
        "log": gitsafe.log(root, 10),
        "worktrees": [
            {"path": Path(w.get("worktree", "")).name, "branch": w.get("branch", "")}
            for w in gitsafe.worktrees(root)
        ],
    }


@router.get("/projects/{project_id}/diff")
async def project_diff(project_id: str, staged: bool = False) -> dict:
    """The working-tree diff, labelled by who made it.

    `pre_existing` marks changes that were already there. The UI must
    never present somebody's own half-finished work as something JARVIS
    did, so the distinction is computed here rather than guessed at in
    the browser.
    """
    try:
        root = projects.resolve_root(project_id)
    except WorkspaceViolation as exc:
        raise HTTPException(status_code=404, detail=exc.reason) from None

    state = gitsafe.status(root)
    jarvis_paths = set()
    for record in tasks.list_tasks(project_id):
        for changed in record.files_changed:
            if changed.get("path"):
                jarvis_paths.add(str(changed["path"]))

    return {
        "diff": gitsafe.diff(root, staged=staged),
        "changed": [
            {"path": path, "changed_by": "jarvis" if path in jarvis_paths else "you"}
            for path in sorted(set(state.staged + state.modified + state.untracked))
        ],
        "jarvis_paths": sorted(jarvis_paths),
    }


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

@router.get("/tasks")
async def list_coding_tasks(project_id: str = "") -> dict:
    return {
        "tasks": [
            {
                "id": r.id, "project_id": r.project_id, "request": r.request,
                "state": r.state, "created_at": r.created_at,
                "files_changed": len(r.files_changed), "steps": len(r.steps),
            }
            for r in tasks.list_tasks(project_id)
        ],
        "interrupted": [r.id for r in tasks.list_tasks() if r.state == tasks.TaskState.INTERRUPTED.value],
    }


@router.get("/tasks/{task_id}")
async def get_coding_task(task_id: str) -> dict:
    record = tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="No such task.")
    return {"task": record.as_dict()}


@router.get("/tasks/{task_id}/report")
async def export_coding_task(task_id: str) -> dict:
    record = tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="No such task.")
    return {"report": tasks.redacted_report(record)}


@router.delete("/tasks/{task_id}", dependencies=[Depends(require_session_token)])
async def delete_coding_task(task_id: str) -> dict:
    if not tasks.delete(task_id):
        raise HTTPException(status_code=404, detail="No such task.")
    return {"deleted": True}


@router.post("/tasks/clear", dependencies=[Depends(require_session_token)])
async def clear_coding_tasks(project_id: str = "") -> dict:
    removed = tasks.clear(project_id)
    return {"removed": removed}


@router.post("/tasks/plan", dependencies=[Depends(require_session_token)])
async def plan_coding_task(body: StartTaskRequest) -> dict:
    """Produce the plan the user approves before anything runs.

    This endpoint performs **no** edits, runs **no** commands and starts
    **no** processes. It reports what the task would do, what it would
    need approval for, and what leaves the device.
    """
    from app.core.privacy import privacy_mode

    try:
        root = projects.resolve_root(body.project_id)
    except WorkspaceViolation as exc:
        raise HTTPException(status_code=404, detail=exc.reason) from None

    detected = stacks.detect(root)
    declared = stacks.project_commands(detected)
    snapshot = gitsafe.snapshot_user_changes(root)
    record = tasks.create(body.project_id, body.request)
    isolation = gitsafe.plan_isolation(root, record.id)

    provider = _provider_disclosure(privacy_mode.active)

    plan = {
        "task_id": record.id,
        "objective": record.request,
        "project": {"id": body.project_id, "name": Path(root).name, "stack": detected.label},
        "isolation": isolation.as_dict(),
        "pre_existing_changes": snapshot.as_dict(),
        "expected_commands": [
            {"intent": intent, "argv": entry["argv"], "source": entry["source"]}
            for intent, entry in declared.items()
        ],
        "missing_commands": stacks.missing_intents(detected),
        "operations_requiring_approval": [
            "Installing, updating or removing any package",
            "Deleting any file",
            "Any command not declared by this project",
            "Any command that reaches the network",
            "Creating a Git commit",
        ],
        "validation_plan": [
            f"Run the project's own '{intent}' command" for intent in
            ("lint", "typecheck", "test", "build") if intent in declared
        ] or ["This project declares no test or build command, so JARVIS cannot verify "
              "its own changes by running anything. It will report that rather than "
              "claim the change was validated."],
        "risk_level": "medium" if isolation.possible else "high",
        "provider": provider,
        "limits": {
            "max_steps": limits.MAX_STEPS,
            "max_commands": limits.MAX_COMMANDS,
            "max_files_edited": limits.MAX_FILES_EDITED,
            "max_minutes": int(limits.MAX_ELAPSED_SECONDS // 60),
        },
    }

    record.plan = plan
    record.isolation = isolation.as_dict()
    record.pre_existing_changes = {**snapshot.as_dict(), "all_paths": snapshot.all_paths}
    record.provider = provider
    tasks.set_state(record, tasks.TaskState.AWAITING_PLAN_APPROVAL)

    return {"plan": plan}


def _provider_disclosure(privacy_active: bool) -> dict:
    """What the user is told before any project content moves.

    Never claims a small local model is up to a large change — the
    `capability_note` says so in plain words rather than letting the
    absence of a warning imply competence.
    """
    from app.core.preferences import load as load_preferences

    preferences = load_preferences()
    provider = preferences.get("ai_provider", "anthropic")
    model = preferences.get("ollama_model", "")

    if privacy_active:
        return {
            "provider": "blocked",
            "location": "none",
            "content_leaves_device": False,
            "note": (
                "Privacy mode is on. JARVIS will not send any project content to a "
                "cloud model. Switch to a local provider, or turn privacy mode off, "
                "to run a coding task."
            ),
            "blocked": True,
        }

    if provider == "ollama":
        return {
            "provider": "ollama",
            "model": model or "(none selected)",
            "location": "local",
            "content_leaves_device": False,
            "note": "Project content stays on this machine.",
            "capability_note": (
                "A small local model may not reliably complete a large or subtle "
                "repository change. JARVIS will report what it could not finish "
                "rather than claim success."
            ),
            "blocked": False,
        }

    return {
        "provider": "anthropic",
        "location": "cloud",
        "content_leaves_device": True,
        "note": (
            "Selected file contents from this project will be sent to Anthropic to "
            "complete the task. Protected files (.env, keys, certificates, .git "
            "internals) are never included."
        ),
        "blocked": False,
    }


@router.post("/tasks/stop", dependencies=[Depends(require_session_token)])
async def stop_coding_task(body: TaskIdRequest) -> dict:
    """Stop a running task and end every process it owns.

    Reports what actually happened to each process rather than assuming
    the signal landed.
    """
    from app.coding import sessions

    result = sessions.stop(body.task_id)
    record = tasks.get(body.task_id)
    if record is not None and record.state not in (
        tasks.TaskState.COMPLETED.value, tasks.TaskState.FAILED.value
    ):
        tasks.set_state(record, tasks.TaskState.STOPPED, {"stopped_by": "user"})
    return result


@router.post("/processes/stop-all", dependencies=[Depends(require_session_token)])
async def stop_all_processes() -> dict:
    """The safety valve: end every command and preview Coding Workspace
    owns, right now."""
    reports = ledger.stop_all("user requested stop-all")
    return {"stopped": len(reports), "reports": reports}
