"""Live task sessions — what is running right now, and how to stop it.

Task *records* (`tasks.py`) are the durable history. This is the volatile
half: the contexts of tasks currently in flight, so Stop can reach them.

In-memory only, and deliberately so — the same reasoning as
`app/core/pending_actions.py`. A task cannot survive a restart, so a
"running" task found in the history after one was interrupted, not
running, and `tasks.mark_interrupted_on_startup()` says so rather than
letting a stale record imply otherwise.

**Stop is total and honest.** It sets the flag the agent loop checks,
ends every owned process tree, stops the preview, and reports what
actually happened to each process — including anything that survived.
It never touches the user's files or Git state: a stopped task leaves
the edits it already made, because those were applied, shown and (where
required) approved. Rolling them back would be undoing work the user
watched happen.
"""

from __future__ import annotations

import threading
from typing import Dict, List

from app.logging_config import get_logger

logger = get_logger("coding.sessions")

_lock = threading.Lock()
_live: Dict[str, object] = {}       # task_id -> TaskRunner


def register(runner) -> None:
    with _lock:
        _live[runner.context.task_id] = runner


def unregister(task_id: str) -> None:
    with _lock:
        _live.pop(task_id, None)


def get(task_id: str):
    with _lock:
        return _live.get(task_id)


def live_ids() -> List[str]:
    with _lock:
        return sorted(_live)


def stop(task_id: str) -> dict:
    """Stop one task completely."""
    runner = get(task_id)
    if runner is None:
        return {
            "stopped": False,
            "reason": "That task is not running.",
            "processes_stopped": 0,
        }

    # `request_stop` sets the flag the loop checks *and* cancels the model
    # request in flight. Setting the flag alone would leave Stop waiting
    # for however long the provider takes to finish answering.
    context = runner.context
    runner.request_stop()

    reports: List[dict] = []
    preview = getattr(context, "preview", None)
    if preview is not None:
        try:
            reports.append(preview.stop("task stopped"))
        except Exception:  # noqa: BLE001 — stopping must not fail
            logger.warning("Preview stop raised during task stop.", exc_info=True)

    from app.coding.runner import ledger
    reports.extend(ledger.stop_all(f"task {task_id} stopped"))

    unregister(task_id)
    logger.info("Coding task %s stopped by request.", task_id)

    survivors = [
        s for report in reports
        for s in (report.get("survivors") or report.get("cleanup", {}).get("survivors") or [])
    ]
    return {
        "stopped": True,
        "task_id": task_id,
        "processes_stopped": len(reports),
        "survivors": survivors,
        "edits_kept": True,
        "message": (
            "Stopped. Changes JARVIS had already made and shown you are kept — "
            "they were applied, not abandoned. Nothing you changed yourself was touched."
        ),
        "reports": reports,
    }


def stop_all(reason: str = "shutdown") -> List[dict]:
    """Called on shutdown. Ends everything Coding Workspace owns."""
    results = []
    for task_id in live_ids():
        results.append(stop(task_id))
    from app.coding.runner import ledger
    ledger.stop_all(reason)
    return results
