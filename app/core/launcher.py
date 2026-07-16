"""Production launcher for the installed Windows app.

Only used by run_jarvis.py when frozen (never by `python -m app.main` or
pytest). Runs everything — the FastAPI/uvicorn server and the caller — in a
single process: uvicorn runs on a background thread while the main thread
waits for the health check, opens the dashboard in the default browser, then
blocks until the server thread exits. There is deliberately no subprocess
here, so there is no child process that could ever be orphaned.

Assumes the frozen executable is built with PyInstaller's windowed/noconsole
mode — this module does not itself hide any console window.
"""

import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from app.core import paths
from app.logging_config import get_logger, setup_logging

logger = get_logger("launcher")


def find_free_port(preferred: int) -> int:
    """Prefer *preferred*; fall back to any free loopback port if it's taken."""
    for port in (preferred, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    raise RuntimeError("No free network port was available on 127.0.0.1.")


def is_process_alive(pid: int) -> bool:
    if pid is None or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except Exception:
        return False


def _lock_path() -> Path:
    return paths.single_instance_lock_path()


def _read_lock() -> Optional[dict]:
    lock_path = _lock_path()
    if not lock_path.exists():
        return None
    try:
        return json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_lock(port: int) -> None:
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8")


def try_acquire_lock(port: int) -> Optional[dict]:
    """None means the lock is ours (we're the only instance). A dict means a
    live instance already holds it — its {pid, port} is returned so the
    caller can bring it to the foreground instead of starting a second
    backend/DB stack."""
    existing = _read_lock()
    if existing and is_process_alive(existing.get("pid", -1)):
        return existing
    _write_lock(port)
    return None


def release_lock() -> None:
    existing = _read_lock()
    if existing and existing.get("pid") == os.getpid():
        try:
            _lock_path().unlink()
        except OSError:
            pass


def wait_for_health(port: int, timeout: float = 20.0, interval: float = 0.3) -> bool:
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(interval)
    return False


def open_browser(port: int, path: str = "/ui/") -> None:
    import webbrowser
    webbrowser.open(f"http://127.0.0.1:{port}{path}")


def show_error_dialog(title: str, message: str) -> None:
    """User-readable failure surface. Never raises — startup failures must
    still be logged even if the dialog itself can't be shown."""
    try:
        if sys.platform == "win32":
            import ctypes
            MB_ICONERROR = 0x10
            ctypes.windll.user32.MessageBoxW(0, message, title, MB_ICONERROR)
        else:
            logger.error("%s: %s", title, message)
    except Exception:
        logger.error("%s: %s", title, message)


def _build_server(host: str, port: int):
    import uvicorn
    from app.api.server import app as fastapi_app
    from app.config import settings as app_settings

    config = uvicorn.Config(
        fastapi_app,
        host=host,
        port=port,
        reload=False,
        log_level=app_settings.jarvis_log_level.lower(),
    )
    return uvicorn.Server(config)


def _serve_in_thread(server) -> threading.Thread:
    thread = threading.Thread(target=server.run, name="jarvis-api", daemon=True)
    thread.start()
    return thread


def _install_signal_handlers(server) -> None:
    def _handle_signal(signum, frame):
        server.should_exit = True

    try:
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
    except (ValueError, OSError):
        pass  # not the main thread, or unsupported on this platform


def run_production() -> int:
    """Entry point called by run_jarvis.py for a frozen build with no flags.

    Returns a process exit code — never raises. Every failure path is logged
    and surfaced via show_error_dialog() before returning.
    """
    paths.seed_production_env()
    setup_logging()
    logger.info("JARVIS production launcher starting.")

    from app.core import migration
    migration_result = migration.migrate_if_needed()
    logger.info("Legacy DB migration check: %s", migration_result.get("status"))

    from app.config import settings as app_settings

    try:
        port = find_free_port(app_settings.jarvis_port)
    except Exception as exc:
        logger.error("Could not find a free port: %s", exc)
        show_error_dialog("JARVIS could not start", "No free network port was available on this computer.")
        return 1

    existing = try_acquire_lock(port)
    if existing is not None:
        logger.info(
            "JARVIS is already running (pid=%s, port=%s) — opening it instead of starting a new instance.",
            existing.get("pid"), existing.get("port"),
        )
        open_browser(existing.get("port", port))
        return 0

    try:
        from app.core import runtime_state
        runtime_state.set_actual_port(port)

        server = _build_server(app_settings.jarvis_host, port)
        thread = _serve_in_thread(server)
        _install_signal_handlers(server)

        if not wait_for_health(port, timeout=20.0):
            logger.error("JARVIS API did not become healthy within the startup timeout.")
            show_error_dialog(
                "JARVIS could not start",
                "JARVIS did not finish starting in time. Check the log folder (see Diagnostics) for details.",
            )
            server.should_exit = True
            thread.join(timeout=5.0)
            return 1

        open_browser(port)
        thread.join()
        return 0
    except Exception as exc:
        logger.exception("JARVIS production launcher failed.")
        show_error_dialog("JARVIS could not start", f"An unexpected error occurred: {exc}")
        return 1
    finally:
        release_lock()
