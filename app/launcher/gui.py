"""Pre-tray startup sequence for the packaged, windowed launcher.

launch() runs: single-instance check -> background server start ->
health-wait -> default-browser open. It never prints to a console (none
exists in a --windowed PyInstaller build) and never lets a raw traceback
reach the user — any failure is logged with a correlation ID and shown
as a native message box naming the safe log location, then the process
exits. On success it returns the RunningServer for the caller (the tray
event loop, see app/launcher/tray.py) to own until shutdown().

Deliberately does not itself start the tray — that keeps this module
testable as a plain function (call it, check what it returned/did)
without dragging in a native event loop.
"""

import sys
import uuid
import webbrowser
from typing import Optional

from app.config import settings
from app.launcher import instance_lock, server_runner
from app.logging_config import get_logger, setup_logging

logger = get_logger("launcher.gui")


def dashboard_url() -> str:
    return f"http://{settings.jarvis_host}:{settings.jarvis_port}/ui/"


def _format_error_message(reason: str, correlation_id: str) -> str:
    return f"{reason}\n\nReference ID: {correlation_id}\nLog file: {settings.log_file}"


def _show_error_dialog(title: str, message: str) -> None:
    if sys.platform == "win32":
        import ctypes
        MB_OK_ICONERROR = 0x00000010
        ctypes.windll.user32.MessageBoxW(None, message, title, MB_OK_ICONERROR)
    else:
        # No native dialog off Windows (dev/CI) — the caller has already
        # logged the same message at ERROR level.
        pass


def _fail(reason: str) -> None:
    correlation_id = str(uuid.uuid4())
    logger.error("Launcher startup failed [%s]: %s", correlation_id, reason)
    _show_error_dialog("JARVIS couldn't start", _format_error_message(reason, correlation_id))
    sys.exit(1)


def launch() -> Optional[server_runner.RunningServer]:
    """Returns the running server on success. Exits the process (via
    _fail -> sys.exit(1)) on any startup failure — callers should treat
    this as always either returning a usable RunningServer or not
    returning at all."""
    setup_logging()

    check = instance_lock.check_existing_instance(settings.jarvis_host, settings.jarvis_port)
    if check.another_instance_running:
        logger.info("JARVIS is already running (pid=%s) — opening its dashboard instead of starting a second copy.", check.pid)
        webbrowser.open(dashboard_url())
        sys.exit(0)

    if check.port_in_use_by_other:
        _fail(
            f"Port {settings.jarvis_port} is already in use by another application "
            "(not JARVIS). Close that application, or change JARVIS_PORT, and try again."
        )
        return None  # unreachable — _fail() exits the process

    instance_lock.acquire_lock()
    running = server_runner.start_server_in_background()

    if not server_runner.wait_until_healthy():
        shutdown(running)
        _fail("JARVIS's local server did not become healthy in time.")
        return None  # unreachable

    logger.info("JARVIS is healthy — opening the dashboard.")
    webbrowser.open(dashboard_url())
    return running


def shutdown(running: server_runner.RunningServer) -> None:
    """Full graceful shutdown: stop uvicorn, release the single-instance
    lock. Idempotent enough to call once from the tray's Quit handler;
    not designed to be called concurrently from two threads."""
    logger.info("JARVIS shutting down.")
    running.request_shutdown()
    instance_lock.release_lock()


def run_windowed() -> None:
    """The complete windowed entry point: launch() the server, then hand
    off to the tray event loop until Quit. This is what run_jarvis.py
    calls — the only place in this package that wires gui and tray
    together, kept out of both modules' own import graphs so importing
    either alone (as most of this package's tests do) never needs pystray."""
    running = launch()
    from app.launcher.tray import run_tray_loop
    run_tray_loop(running, settings.jarvis_host, settings.jarvis_port)
