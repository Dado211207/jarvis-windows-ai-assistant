"""Starting a task, and the four things that must be true before one runs.

The HTTP layer should not be where "may this task start" is decided, so
it is decided here:

1. The plan was approved. A task starts from `AWAITING_PLAN_APPROVAL`
   and no other state. There is no endpoint that skips the plan.
2. A provider is actually usable. Not "configured" — usable, right now,
   with the reason reported when it is not. Privacy mode plus a cloud
   provider is refused here and named as such, never quietly downgraded
   to a local one.
3. Isolation succeeded, or the user was told exactly why it could not
   and chose to continue anyway. §6 of the brief is explicit: if
   isolation is not safe, stop, explain, propose a non-destructive
   alternative, and do not continue automatically. `allow_in_place` is
   that explicit choice, and it is a separate parameter precisely so it
   cannot be arrived at by default.
4. The user's own uncommitted work was recorded first, so the Diff panel
   can tell their changes from JARVIS's for the whole life of the task.

The task then runs on a daemon thread. The HTTP request returns
immediately: a coding task takes minutes, and a request handler that
blocks for minutes is one the window cannot cancel.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from app.coding import agent, gitsafe, limits, loop, projects, sessions, stacks, tasks
from app.coding.workspace import WorkspaceViolation
from app.logging_config import get_logger

logger = get_logger("coding.service")


class StartRefused(Exception):
    """A task may not start. Carries a sentence the user can act on."""

    def __init__(self, reason: str, detail: Optional[dict] = None) -> None:
        self.reason = reason
        self.detail = detail or {}
        super().__init__(reason)


def start_task(task_id: str, *, allow_in_place: bool = False) -> dict:
    """Begin an approved task. Returns as soon as it is running."""
    record = tasks.get(task_id)
    if record is None:
        raise StartRefused("No such task.")

    if record.state != tasks.TaskState.AWAITING_PLAN_APPROVAL.value:
        raise StartRefused(
            f"This task is '{record.state}', not waiting for plan approval. "
            "Start a new task rather than restarting this one."
        )

    if sessions.get(task_id) is not None:
        raise StartRefused("That task is already running.")

    try:
        project_root = projects.resolve_root(record.project_id)
    except WorkspaceViolation as exc:
        raise StartRefused(exc.reason) from None

    # 2 — a provider that works, or an honest refusal.
    provider, choice = loop.resolve_provider()
    if provider is None or not choice.ready:
        raise StartRefused(
            choice.reason or "No AI provider is available for coding tasks right now.",
            {"provider": choice.as_dict()},
        )

    # 4 — record the user's own work before anything is created.
    snapshot = gitsafe.snapshot_user_changes(project_root)
    record.pre_existing_changes = {**snapshot.as_dict(), "all_paths": snapshot.all_paths}

    # 3 — isolate, or refuse and explain.
    plan = gitsafe.plan_isolation(project_root, task_id)
    working_root = project_root
    isolation = plan.as_dict()

    if plan.possible:
        created, message = gitsafe.create_worktree(project_root, plan)
        if not created:
            raise StartRefused(
                f"JARVIS did not start: {message} Nothing was changed. You can "
                "commit or stash your current work and try again, or start the "
                "task with isolation turned off if you accept that JARVIS will "
                "edit your working copy directly.",
                {"isolation": isolation},
            )
        working_root = Path(plan.worktree_path or project_root)
        isolation["active"] = True
        isolation["message"] = message
    elif not allow_in_place:
        raise StartRefused(
            plan.reason + " JARVIS has not started and has changed nothing. You can "
            "either resolve that and try again, or start the task again with "
            "'work directly in my folder' selected — in which case JARVIS edits "
            "your files in place, shows every change as a diff, and still never "
            "discards anything you changed yourself.",
            {"isolation": isolation, "can_continue_in_place": plan.strategy == "in_place"},
        )
    else:
        if plan.strategy != "in_place":
            # A blocker like an unresolved merge conflict is not something
            # the user can wave through — continuing would inherit it.
            raise StartRefused(plan.reason, {"isolation": isolation})
        isolation["active"] = False
        isolation["message"] = (
            "Working directly in your folder, at your request. Every change is "
            "shown as a diff before and after, and nothing you changed yourself "
            "is touched."
        )

    detected = stacks.detect(working_root)
    declared = stacks.project_commands(detected)

    context = agent.TaskContext(
        task_id=task_id,
        project_id=record.project_id,
        root=working_root,
        project_root=project_root,
        record=record,
        declared_commands=declared,
        budget=limits.TaskBudget(),
    )

    runner = loop.TaskRunner(
        context,
        provider=provider,
        choice=choice,
        system_prompt=loop.build_coding_prompt(detected.label, project_root.name),
    )
    runner.seed(_opening_message(record, detected, isolation))

    record.isolation = isolation
    record.provider = choice.as_dict()
    tasks.set_state(record, tasks.TaskState.RUNNING)

    sessions.register(runner)
    thread = threading.Thread(
        target=_run_and_report, args=(runner,), name=f"coding-{task_id[:8]}", daemon=True
    )
    thread.start()

    return {
        "started": True,
        "task_id": task_id,
        "isolation": isolation,
        "provider": choice.as_dict(),
        "working_in": "worktree" if isolation.get("active") else "your project folder",
    }


def _opening_message(record, detected, isolation: dict) -> str:
    """The only text in the conversation that carries authority.

    Everything else the model reads arrives inside an untrusted-content
    envelope. This one does not, and it is assembled here from the user's
    own request and JARVIS's own findings — never from a project file.
    """
    return (
        f"TASK FROM THE USER: {record.request}\n\n"
        f"Project stack: {detected.label}\n"
        f"Package manager: {detected.package_manager or 'not detected'}\n"
        f"Declared commands: {', '.join(sorted(detected.commands)) or 'none'}\n"
        f"Working location: {isolation.get('message', 'the project folder')}\n\n"
        "Start by looking at the project before changing anything. "
        "Reply with the JSON described in your instructions."
    )


def _run_and_report(runner) -> None:
    """The thread body. Must never let an exception escape.

    An unhandled exception on a daemon thread in a windowed build is a
    task that stops with no record of why — the user sees 'running'
    forever. Every ending is written down, including this one.
    """
    try:
        result = runner.run()
        logger.info("Coding task %s ended: %s", runner.context.task_id, result.state.value)
    except Exception:  # noqa: BLE001
        logger.warning("A coding task ended with an unexpected error.", exc_info=True)
        try:
            tasks.append_step(
                runner.context.record, "error",
                "The task stopped because of an internal error. Changes already "
                "made are kept and shown in the diff.", {}, ok=False,
            )
            tasks.set_state(runner.context.record, tasks.TaskState.FAILED,
                            {"summary": "Stopped by an internal error."})
        except Exception:  # noqa: BLE001
            logger.warning("Could not record the failure of a coding task.", exc_info=True)
    finally:
        sessions.unregister(runner.context.task_id)


def decide(task_id: str, granted: bool) -> dict:
    """Approve or decline whatever the task is waiting on."""
    runner = sessions.get(task_id)
    if runner is None:
        raise StartRefused("That task is not running.")
    if runner.pending_approval is None:
        raise StartRefused("That task is not waiting for a decision.")

    # The continuation runs on its own thread for the same reason the
    # task does: approving an install should not block the browser for
    # however long npm takes.
    result_holder: dict = {}

    def _continue() -> None:
        try:
            result = runner.approve(granted)
            result_holder["state"] = result.state.value
        except Exception:  # noqa: BLE001
            logger.warning("Continuing after an approval failed.", exc_info=True)
        finally:
            if runner.state in (loop.LoopState.COMPLETED, loop.LoopState.FAILED,
                                loop.LoopState.LIMIT, loop.LoopState.STOPPED):
                sessions.unregister(task_id)

    threading.Thread(target=_continue, name=f"coding-approve-{task_id[:8]}",
                     daemon=True).start()
    return {"recorded": True, "granted": granted, "task_id": task_id}


def live_state(task_id: str) -> dict:
    """What is happening right now, distinguished from what is recorded.

    A record says a task is RUNNING; only the live session can say
    whether it really is. When there is no session, the answer is
    "not running" — never the stored flag.
    """
    runner = sessions.get(task_id)
    record = tasks.get(task_id)
    if runner is None:
        return {
            "live": False,
            "state": record.state if record else "unknown",
            "pending_approval": None,
            "preview": None,
            "budget": None,
        }

    preview = getattr(runner.context, "preview", None)
    return {
        "live": True,
        "state": runner.state.value,
        "pending_approval": runner.pending_approval,
        "preview": preview.state.as_dict() if preview is not None else None,
        "budget": runner.context.budget.remaining(),
        "elapsed_seconds": round(runner.context.elapsed(), 1),
        "provider": runner.choice.as_dict(),
    }
