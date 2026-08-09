"""Cleaning up the processes a child of ours left behind.

WebView2 hosts its browser and GPU processes as children of whatever
launched the control, and they do not necessarily die with it. Kill the
window child without dealing with them and the user sees "JARVIS" still
listed in Task Manager after choosing Quit — which is exactly the defect
reported from real Windows hardware.

Two rules this module never breaks:

* **Only our own descendants.** PIDs are captured by walking down from a
  process *this launcher started*, never by matching on a name. A
  name-based sweep would happily terminate an unrelated msedge the user
  was browsing in.
* **Captured before the kill.** Once the parent is gone, the
  parent/child relationship that identifies its descendants is gone with
  it, so the capture has to happen while it is still alive.

Every function here is best-effort and total: psutil may be absent, a
process may vanish mid-walk, a PID may already have been recycled.
Tidying up leftovers must never be able to break shutdown, so nothing
raises.
"""

from typing import List, Optional

from app.logging_config import get_logger

logger = get_logger("launcher.process_tree")

TERMINATE_GRACE_SECONDS = 3.0


def descendant_pids(pid: Optional[int]) -> List[int]:
    """PIDs descended from *pid*, captured while it is still alive."""
    if pid is None:
        return []
    try:
        import psutil

        return [child.pid for child in psutil.Process(pid).children(recursive=True)]
    except Exception:  # noqa: BLE001 — best-effort cleanup only
        return []


def terminate_pids(pids: List[int]) -> None:
    """Terminates processes previously captured by descendant_pids(),
    escalating to kill for any that ignore it."""
    if not pids:
        return
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return

    survivors = []
    for pid in pids:
        try:
            process = psutil.Process(pid)
            process.terminate()
            survivors.append(process)
        except Exception:  # noqa: BLE001 — already gone, or not ours to touch
            continue

    if not survivors:
        return
    try:
        _, alive = psutil.wait_procs(survivors, timeout=TERMINATE_GRACE_SECONDS)
        for process in alive:
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return


def terminate_descendants_of(pid: Optional[int]) -> None:
    """Capture-then-terminate in one call, for the common case where the
    parent is already gone and only its leftovers remain."""
    terminate_pids(descendant_pids(pid))
