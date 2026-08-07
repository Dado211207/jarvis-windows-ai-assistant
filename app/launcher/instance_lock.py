"""Single-instance detection for the packaged launcher.

Deliberately a PID lock file, not a Windows named mutex: a mutex would
only ever be exercisable on windows-latest CI, whereas this is fully
unit-testable on any platform via psutil (already a dependency). The
lock file lives under app_paths.app_data_root(), so it is itself
per-user and per-machine, matching where everything else mutable lives.

Honest limitation: like any lock-file scheme, there is a small window
between deciding "we are the primary instance" and the lock file
actually being written where two processes launched at the same instant
could both proceed. This is not closed by a hardware-atomic primitive
here. In practice this matters only for near-simultaneous double
double-clicks, and the port bind performed by the server itself is the
final backstop — a second server would fail to bind and surface as a
health-wait timeout rather than silently running two servers.
"""

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil

from app.core.app_paths import app_data_root
from app.logging_config import get_logger

logger = get_logger("launcher.instance_lock")

LOCK_FILE_NAME = "jarvis.lock"


def lock_file_path() -> Path:
    return app_data_root() / LOCK_FILE_NAME


@dataclass
class InstanceCheckResult:
    another_instance_running: bool
    port_in_use_by_other: bool
    pid: Optional[int] = None


def _process_looks_like_jarvis(pid: int) -> bool:
    """Best-effort extra signal against a PID being reused by an
    unrelated process after a crash — not a strong identity guarantee.
    Matches the packaged process name (JARVIS.exe) or a dev-mode
    interpreter (python/python3), since dev mode runs the same launcher
    code under a plain Python process."""
    try:
        name = psutil.Process(pid).name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    return "jarvis" in name or name.startswith("python")


def _port_is_bound(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _read_locked_pid(path: Path) -> Optional[int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        return pid if pid > 0 else None
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def check_existing_instance(host: str, port: int) -> InstanceCheckResult:
    """Decide whether it's safe to become the primary JARVIS instance.

    Order matters: a lock held by a live, JARVIS-looking process always
    wins over the port check, since if it *is* JARVIS, the port being
    bound is expected, not a conflict. Only checks the port once the
    lock has been ruled out (missing or stale).
    """
    path = lock_file_path()

    if path.exists():
        pid = _read_locked_pid(path)
        if pid is not None and psutil.pid_exists(pid) and _process_looks_like_jarvis(pid):
            return InstanceCheckResult(another_instance_running=True, port_in_use_by_other=False, pid=pid)

        logger.info("Clearing stale instance lock (pid=%s no longer a live JARVIS process).", pid)
        try:
            path.unlink()
        except OSError:
            pass

    if _port_is_bound(host, port):
        return InstanceCheckResult(another_instance_running=False, port_in_use_by_other=True)

    return InstanceCheckResult(another_instance_running=False, port_in_use_by_other=False)


def acquire_lock() -> None:
    path = lock_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")


def release_lock() -> None:
    try:
        lock_file_path().unlink(missing_ok=True)
    except OSError:
        pass
