"""Coding Workspace HTTP surface.

Every browser-facing endpoint is session-token protected. Coding reads
expose project paths, diffs, task output, screenshots and live preview
state, so every GET carries the same local-dashboard dependency as ordinary
mutations. The native folder-dialog callback keeps its separate inherited
desktop-secret gate. Structural invariants fail if a future Coding or Voice
integration GET loses authentication.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.api.session import require_session_token
from app.coding import commands as command_policy
from app.coding import gitsafe, limits, projects, registry, stacks, tasks
from app.coding.runner import ledger
from app.coding.workspace import WorkspaceViolation, protected_summary
from app.core.errors import ErrorCategory, to_safe_error
from app.logging_config import get_logger

logger = get_logger("api.coding")

router = APIRouter(prefix="/coding", tags=["coding"])


# --------------------------------------------------------------------------
# Request models — every mutating body is typed and bounded.
# --------------------------------------------------------------------------

class AddProjectRequest(BaseModel):
    # Either a folder the native picker returned (`request_id`) or one
    # typed into the labelled fallback field (`path`). Both are validated
    # identically by `workspace.canonical_root`; the difference is only
    # that the server can tell them apart.
    path: str = Field(default="", max_length=4000)
    request_id: str = Field(default="", max_length=128)
    name: str = Field(default="", max_length=120)


class PlanProjectRequest(BaseModel):
    """Step one of two. Produces a plan; writes nothing."""

    parent_path: str = Field(default="", max_length=4000)
    # A folder the native picker returned, spent here instead of a typed
    # path. Exactly one of the two is used, and the server can tell which.
    parent_request_id: str = Field(default="", max_length=128)
    name: str = Field(min_length=1, max_length=80)
    template: str = Field(default="static", max_length=40)


class CreateProjectRequest(BaseModel):
    """Step two. Nothing is written without a plan id."""

    plan_id: str = Field(min_length=1, max_length=64)


class FolderDialogRequest(BaseModel):
    purpose: str = Field(min_length=1, max_length=40)


class FolderDialogResult(BaseModel):
    """What the native window reports back. One of three outcomes."""

    path: str = Field(default="", max_length=4000)
    cancelled: bool = False
    error: str = Field(default="", max_length=300)


class StartTaskRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    request: str = Field(min_length=1, max_length=4000)


class TaskIdRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)


class StartApprovedTaskRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)
    # Deliberately separate, and deliberately defaulting to False: §6 of
    # the brief requires that when isolation is not safe JARVIS stops and
    # explains rather than continuing. Working in the user's own folder
    # has to be something they chose, not something that happened.
    allow_in_place: bool = False


class ApprovalDecisionRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)
    granted: bool


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

@router.get("/status", dependencies=[Depends(require_session_token)])
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
        # Rendered by the page rather than written into the template, so
        # what the user is shown comes from the same frozensets
        # is_protected() enforces and cannot drift from them.
        "protected": protected_summary(),
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


@router.get("/projects", dependencies=[Depends(require_session_token)])
async def list_coding_projects() -> dict:
    return {"projects": [_project_payload(p) for p in projects.list_projects()]}


@router.post("/projects", dependencies=[Depends(require_session_token)])
async def add_coding_project(body: AddProjectRequest) -> dict:
    from app.coding import folder_requests

    path = body.path
    picked = False
    if body.request_id:
        # A folder the person chose in the native dialog. Spent here, once.
        try:
            path = folder_requests.broker.consume(body.request_id)
            picked = True
        except folder_requests.FolderRequestError as exc:
            raise HTTPException(status_code=400, detail=exc.reason) from None
    if not path:
        raise HTTPException(status_code=400, detail="No folder was given.")

    try:
        project = projects.add(path, body.name)
    except WorkspaceViolation as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from None
    except Exception as exc:  # noqa: BLE001
        raise _safe_failure(exc, "add coding project") from None
    # Reported so the page cannot claim a folder was chosen through the
    # picker when it was typed. Only the server knows which happened.
    return {"project": _project_payload(project), "selected_via_picker": picked}


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


@router.post("/projects/plan", dependencies=[Depends(require_session_token)])
async def plan_coding_project(body: PlanProjectRequest) -> dict:
    """Step one of two: what creating this project *would* do.

    Reads the filesystem and writes nothing — see
    `app/coding/project_plan.py`, whose test walks its AST to prove there
    is no write in it. The plan is what the confirmation screen shows.
    """
    from app.coding import folder_requests, project_plan

    parent = body.parent_path
    picked = False
    if body.parent_request_id:
        try:
            parent = folder_requests.broker.consume(body.parent_request_id)
            picked = True
        except folder_requests.FolderRequestError as exc:
            raise HTTPException(status_code=400, detail=exc.reason) from None
    if not parent:
        raise HTTPException(status_code=400, detail="No parent folder was given.")

    try:
        plan = project_plan.build_plan(parent, body.name, body.template)
    except (project_plan.PlanError, WorkspaceViolation) as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from None
    except Exception as exc:  # noqa: BLE001
        raise _safe_failure(exc, "plan a coding project") from None
    return {"plan": plan.as_dict(), "selected_via_picker": picked}


@router.post("/projects/plan/{plan_id}/cancel", dependencies=[Depends(require_session_token)])
async def cancel_coding_project_plan(plan_id: str) -> dict:
    """Throw a plan away. There is nothing to undo, because nothing was
    written — which is the whole point of the plan step."""
    from app.coding import project_plan

    return {"cancelled": project_plan.cancel(plan_id), "files_created": 0}


@router.post("/projects/create", dependencies=[Depends(require_session_token)])
async def create_coding_project(body: CreateProjectRequest) -> dict:
    """Step two of two: create the project a plan describes.

    Takes a plan id and nothing else. There is deliberately no path,
    name or template on this request — a create that could be given its
    own destination would be a one-step create with an extra field, and
    the plan the user read would not be the thing that ran.

    The plan is re-checked against the filesystem immediately before
    anything is written; a destination that appeared while the
    confirmation was on screen is a refusal, not an overwrite.
    """
    from app.coding import project_plan

    plan = project_plan.get(body.plan_id)
    if plan is None:
        raise HTTPException(
            status_code=400,
            detail="That plan is no longer available. Review the details and confirm again.",
        )
    try:
        project = project_plan.execute(plan)
    except (project_plan.PlanError, WorkspaceViolation) as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from None
    except Exception as exc:  # noqa: BLE001
        raise _safe_failure(exc, "create coding project") from None
    return {"project": _project_payload(project), "plan_id": plan.id}


# --------------------------------------------------------------------------
# The native folder dialog
#
# Brokered, because only the process that owns a window can put a modal on
# it, and that is not this one. See app/coding/folder_requests.py for the
# whole flow and why the answer comes back through an authenticated
# endpoint rather than through the page.
# --------------------------------------------------------------------------

@router.get("/folder-dialog", dependencies=[Depends(require_session_token)])
async def folder_dialog_availability() -> dict:
    """Whether a native folder dialog can be opened right now.

    Answered from the desktop-ready signal, which is the parent's *proved*
    account of whether a window is alive — not a guess from this process,
    which has no window of its own and cannot see one.
    """
    from app.api.routes import _desktop_ready_state

    window_alive = bool((_desktop_ready_state or {}).get("window_alive"))
    return {
        "available": window_alive,
        "reason": "" if window_alive else (
            "JARVIS is running without its native window, so Windows cannot show a "
            "folder dialog. Type the folder path instead."
        ),
    }


@router.post("/folder-dialog", dependencies=[Depends(require_session_token)])
async def open_folder_dialog(body: FolderDialogRequest) -> dict:
    from app.coding import folder_requests

    try:
        request = folder_requests.broker.create(body.purpose)
    except folder_requests.FolderRequestError as exc:
        # 409 means "not right now" (a dialog is already open); 400 means
        # "not ever" (that is not a folder JARVIS asks for). A page that
        # sees them as one cannot tell whether retrying would help.
        raise HTTPException(status_code=409 if exc.conflict else 400,
                            detail=exc.reason) from None
    return {"request": request.as_dict(include_path=True)}


@router.get("/folder-dialog/{request_id}", dependencies=[Depends(require_session_token)])
async def read_folder_dialog(request_id: str) -> dict:
    from app.coding import folder_requests

    request = folder_requests.broker.get(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="That folder request is not open.")
    return {"request": request.as_dict(include_path=True)}


@router.post("/folder-dialog/{request_id}/cancel",
             dependencies=[Depends(require_session_token)])
async def cancel_folder_dialog(request_id: str) -> dict:
    from app.coding import folder_requests

    request = folder_requests.broker.cancel(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="That folder request is not open.")
    return {"request": request.as_dict(include_path=True)}


@router.post("/folder-dialog/{request_id}/result")
async def report_folder_dialog(
    request_id: str,
    body: FolderDialogResult,
    x_jarvis_desktop_secret: Optional[str] = Header(default=None),
) -> dict:
    """The native window reporting what the person chose.

    Deliberately **not** behind the session token: the caller is the
    window child, which has no browser session. It authenticates with the
    per-session desktop secret that only the processes JARVIS started
    inherited, so nothing else on this machine can claim a folder was
    picked. A server started without one refuses every call rather than
    accepting any.
    """
    from app.api.routes import _require_desktop_secret
    from app.coding import folder_requests

    _require_desktop_secret(x_jarvis_desktop_secret)
    try:
        request = folder_requests.broker.resolve(
            request_id, path=body.path, cancelled=body.cancelled, error=body.error)
    except folder_requests.FolderRequestError as exc:
        raise HTTPException(status_code=409 if exc.conflict else 400,
                            detail=exc.reason) from None
    # The window does not need the path back; it is the thing that sent it.
    return {"request": request.as_dict(include_path=False)}


@router.get("/templates", dependencies=[Depends(require_session_token)])
async def list_templates() -> dict:
    from app.coding import templates
    return {"templates": templates.describe_all()}


# --------------------------------------------------------------------------
# Project inspection
# --------------------------------------------------------------------------

@router.get("/projects/{project_id}/git", dependencies=[Depends(require_session_token)])
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


@router.get("/projects/{project_id}/diff", dependencies=[Depends(require_session_token)])
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
            if changed.get("kind") == "rename" and changed.get("destination"):
                jarvis_paths.add(str(changed["destination"]))

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

@router.get("/tasks", dependencies=[Depends(require_session_token)])
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


@router.get("/tasks/{task_id}", dependencies=[Depends(require_session_token)])
async def get_coding_task(task_id: str) -> dict:
    record = tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="No such task.")
    return {"task": record.as_dict()}


@router.get("/tasks/{task_id}/report", dependencies=[Depends(require_session_token)])
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
            "Every test, lint, format, typecheck, build or dev script declared by this project",
            "Any other development command",
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

    Asks `loop.resolve_provider()` — the same call the task itself makes
    — rather than reading preferences and describing what they imply.
    An earlier version did the latter, which meant the plan could say
    "Anthropic, content leaves your device" while the task then ran on
    Ollama, or vice versa. A disclosure that is computed separately from
    the thing it discloses is a disclosure that can be wrong.

    Never claims a small local model is up to a large change — the
    `capability_note` says so in plain words rather than letting the
    absence of a warning imply competence.
    """
    from app.coding import loop

    _, choice = loop.resolve_provider()
    payload = choice.as_dict()
    payload["blocked"] = not choice.ready
    payload["note"] = payload.pop("privacy_note", "")
    if privacy_active and choice.is_cloud:
        payload["note"] = (
            "Privacy mode is on. JARVIS will not send any project content to a "
            "cloud model. Switch to a local provider, or turn privacy mode off, "
            "to run a coding task."
        )
    return payload


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
    _previews.clear()
    reports = ledger.stop_all("user requested stop-all")
    return {"stopped": reports, "count": len(reports), "reports": reports}


# --------------------------------------------------------------------------
# The preview, and checking it
#
# Startable without a task, because a person looking at their own project
# should be able to say "show me" without asking a model to do it first —
# and because the packaged acceptance test has to prove browser QA works
# in the installed product without calling a cloud API to get there.
#
# It is the same `PreviewSession` a task uses, with the same rules: only a
# script the project itself declares, only loopback, never a process
# JARVIS did not start.
# --------------------------------------------------------------------------

#: One preview per project. A dict rather than a single slot because a
#: person may have two projects open in two tabs, and stopping one must
#: not stop the other.
_previews: dict = {}


class PreviewStartRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    script: str = Field(default="dev", max_length=60)


class PreviewCheckRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    # A path inside the preview. `browser_origin.safe_route` refuses
    # anything that is not one, so there is no URL a caller can name here.
    route: str = Field(default="/", max_length=512)


def _preview_for(project_id: str):
    from app.coding.preview import PreviewSession

    session = _previews.get(project_id)
    if session is None:
        session = PreviewSession()
        _previews[project_id] = session
    return session


@router.post("/preview/start", dependencies=[Depends(require_session_token)])
async def start_preview(body: PreviewStartRequest) -> dict:
    """Start the project's own declared development server.

    JARVIS never guesses a command. A project that declares no dev script
    gets a refusal naming that fact, not an invented `npm start`.
    """
    from app.coding import stacks

    try:
        root = projects.resolve_root(body.project_id)
    except WorkspaceViolation as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from None

    declared = stacks.project_commands(stacks.detect(root))
    entry = declared.get(body.script) or declared.get("dev")
    if entry is None:
        raise HTTPException(
            status_code=400,
            detail=("This project does not declare a development-server script, so "
                    "JARVIS has nothing safe to start. It will not guess a command."),
        )

    session = _preview_for(body.project_id)
    state = session.start(root, list(entry["argv"]), body.script)
    return {"preview": state.as_dict(), "evidence": entry.get("evidence", "")}


@router.post("/preview/stop", dependencies=[Depends(require_session_token)])
async def stop_preview(body: PreviewStartRequest) -> dict:
    session = _previews.pop(body.project_id, None)
    if session is None:
        return {"stopped": False, "reason": "nothing was running"}
    return session.stop("stopped from the Preview panel")


@router.get("/preview/{project_id}", dependencies=[Depends(require_session_token)])
async def read_preview(project_id: str) -> dict:
    session = _previews.get(project_id)
    if session is None:
        from app.coding.preview import PreviewState
        return {"preview": PreviewState().as_dict()}
    return {"preview": session.state.as_dict()}


@router.post("/preview/check", dependencies=[Depends(require_session_token)])
async def check_preview(body: PreviewCheckRequest) -> dict:
    """Open one route of the owned preview in a real browser.

    Takes a project id and a path — never a URL. The origin is computed
    from the port the owned `PreviewSession` bound, and
    `app/coding/browser_origin.py` is the only thing that decides what may
    be opened.
    """
    from app.coding import browser_qa

    session = _previews.get(body.project_id)
    if session is None:
        raise HTTPException(status_code=400,
                            detail="No preview is running for this project.")

    findings = browser_qa.run_checks(session, body.route,
                                     task_id=f"preview{body.project_id[:8]}")
    session._state.console_errors = findings.console_errors
    session._state.failed_requests = findings.failed_requests
    session._state.browser_checked = findings.available
    session._state.browser_state = findings.state.value
    session._state.browser_reason = findings.reason
    session._state.browser = findings.as_dict()
    session._state.screenshot = findings.screenshot
    return {"browser": findings.as_dict(), "preview": session.state.as_dict()}


# --------------------------------------------------------------------------
# Running a task
# --------------------------------------------------------------------------

@router.post("/tasks/start", dependencies=[Depends(require_session_token)])
async def start_coding_task(body: StartApprovedTaskRequest) -> dict:
    """Approve the plan and begin.

    Returns as soon as the task is running, not when it finishes: a
    coding task takes minutes, and a request the window cannot cancel is
    a window that appears to have hung.
    """
    from app.coding import service

    try:
        return service.start_task(body.task_id, allow_in_place=body.allow_in_place)
    except service.StartRefused as exc:
        raise HTTPException(status_code=409, detail=exc.reason,
                            headers=None) from None
    except Exception as exc:  # noqa: BLE001
        raise _safe_failure(exc, "start coding task") from None


@router.post("/tasks/decide", dependencies=[Depends(require_session_token)])
async def decide_coding_approval(body: ApprovalDecisionRequest) -> dict:
    """Approve or decline what a running task is waiting on.

    A grant is recorded against the exact argv or path that was shown.
    Approving `npm install left-pad` does not approve `npm install`, and
    does not carry into the next task.
    """
    from app.coding import service

    try:
        return service.decide(body.task_id, body.granted)
    except service.StartRefused as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from None
    except Exception as exc:  # noqa: BLE001
        raise _safe_failure(exc, "record a coding approval") from None


@router.get("/tasks/{task_id}/live", dependencies=[Depends(require_session_token)])
async def coding_task_live(task_id: str) -> dict:
    """What is happening right now — from the live session, not the record.

    A stored state of "running" survives a restart; a running task does
    not. When there is no live session the answer is "not running", so
    the page cannot show a spinner for a task that stopped existing.
    """
    from app.coding import service

    return service.live_state(task_id)


class CommitRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)
    approved: bool = False


@router.post("/tasks/commit", dependencies=[Depends(require_session_token)])
async def commit_task_changes(body: CommitRequest) -> dict:
    """Commit the task's own changes, locally, after explicit approval.

    Nothing is pushed. There is no endpoint in this version that pushes,
    opens a pull request, merges or deploys — see `GET /coding/status`'s
    `disabled_in_this_version`.
    """
    record = tasks.get(body.task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="No such task.")

    root = Path(record.isolation.get("worktree_path") or "")
    if not root.is_dir():
        try:
            root = projects.resolve_root(record.project_id)
        except WorkspaceViolation as exc:
            raise HTTPException(status_code=404, detail=exc.reason) from None

    paths = []
    for change in record.files_changed:
        if change.get("path"):
            paths.append(str(change["path"]))
        if change.get("kind") == "rename" and change.get("destination"):
            paths.append(str(change["destination"]))
    paths = sorted(set(paths))
    if not paths:
        raise HTTPException(status_code=400, detail="This task changed no files.")

    proposal = gitsafe.build_commit_proposal(root, body.message, paths)
    if not body.approved:
        return {"committed": False, "proposal": proposal.as_dict(),
                "message": "Nothing was committed. Approve the proposal to commit."}

    committed, message = gitsafe.commit(root, proposal, approved=True)
    tasks.append_step(record, "note", message, {"committed": committed}, ok=committed)
    tasks.save(record)
    return {"committed": committed, "message": message, "pushed": False}


class UndoRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)


@router.post("/tasks/undo", dependencies=[Depends(require_session_token)])
async def undo_task_changes(body: UndoRequest) -> dict:
    """Undo JARVIS's own uncommitted changes from this task, and nothing else.

    Every file is checked against the hash JARVIS last wrote before it is
    touched. A file the user has edited since does not match, and is left
    exactly as it is — reported as skipped rather than reverted.
    """
    record = tasks.get(body.task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="No such task.")

    root = Path(record.isolation.get("worktree_path") or "")
    if not root.is_dir():
        try:
            root = projects.resolve_root(record.project_id)
        except WorkspaceViolation as exc:
            raise HTTPException(status_code=404, detail=exc.reason) from None

    results = gitsafe.undo_task_edits(
        root, record.id, list(record.files_changed)
    )
    reverted = [r for r in results if r.get("reverted")]
    skipped = [r for r in results if not r.get("reverted")]
    tasks.append_step(record, "note",
                      f"Undid {len(reverted)} of JARVIS's own change(s).",
                      {"reverted": len(reverted), "skipped": len(skipped)})
    tasks.save(record)
    return {
        "reverted": reverted,
        "skipped": skipped,
        "message": (
            f"{len(reverted)} file(s) put back. "
            + (f"{len(skipped)} left alone because they changed after JARVIS wrote them."
               if skipped else "")
        ),
    }


@router.get("/screenshots/{name}", dependencies=[Depends(require_session_token)])
async def coding_screenshot(name: str):
    """Serve one browser-check screenshot.

    The name is matched against the directory listing rather than joined
    onto a path, so there is no string a caller can supply that reaches a
    file JARVIS did not write there.
    """
    from fastapi.responses import FileResponse

    from app.coding import browser_qa

    directory = browser_qa.screenshot_dir()
    if directory is None:
        raise HTTPException(status_code=404, detail="No screenshots are available.")
    for candidate in directory.glob("*.png"):
        if candidate.name == name:
            return FileResponse(str(candidate), media_type="image/png")
    raise HTTPException(status_code=404, detail="No such screenshot.")


@router.get("/toolchain", dependencies=[Depends(require_session_token)])
async def coding_toolchain(project_id: str = "") -> dict:
    """What is installed on this machine, and what cannot run without it.

    Read-only in the strongest sense: every probe is `--version` on a
    resolved executable. Nothing is installed, nothing is configured, and
    the lookup is bounded — PATH plus a small named set of standard
    locations, never a disk scan.
    """
    from app.coding import toolchain

    root = None
    if project_id:
        try:
            root = projects.resolve_root(project_id)
        except (WorkspaceViolation, Exception):  # noqa: BLE001
            root = None
    return toolchain.diagnose(root)


@router.get("/browser-check", dependencies=[Depends(require_session_token)])
async def browser_check_availability() -> dict:
    """Whether real browser checks can run in this build."""
    from app.coding import browser_qa

    return browser_qa.availability().as_dict()
