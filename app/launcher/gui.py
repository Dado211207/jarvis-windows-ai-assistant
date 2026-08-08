"""Parent launcher: the authoritative JARVIS application lifecycle owner.

Process layout, and why it is this way:

    parent JARVIS.exe        tray message loop on the MAIN thread
      |                      single-instance lock, restart/quit/recovery
      +-- JARVIS.exe --api      the FastAPI/uvicorn runtime (loopback only)
      +-- JARVIS.exe --window   pywebview, owning that child's main thread

pywebview must own its process's main thread (winforms.py calls
signal.signal(), main-thread-only in CPython). So must a reliable Win32
tray message loop. When pywebview won that contest inside one process, no
window belonged to the parent's main thread and a graceful `taskkill` —
the mechanism behind Windows' "End task" and Inno Setup's
CloseApplications=yes — stopped being delivered reliably: one pass and
two failures across three consecutive runs on identical code. Giving each
its own process ends the contest instead of arbitrating it. See
docs/desktop-shell-findings.md for the full evidence.

Startup order is a hard sequence, not an optimisation: lock -> secret ->
server child -> real health -> window child -> tray loop. The window is
never started against a server that has not proven itself healthy, so it
can never show a misleading connected state at startup.
"""

import os
import sys
import uuid
import webbrowser
from typing import Optional

from app.config import settings
from app.launcher import boot_trace, instance_lock, server_process, window_process
from app.logging_config import get_logger, setup_logging

logger = get_logger("launcher.gui")

SERVER_HEALTH_TIMEOUT_SECONDS = 30.0
SERVER_STOP_TIMEOUT_SECONDS = 8.0
WINDOW_STOP_TIMEOUT_SECONDS = 6.0


def dashboard_url() -> str:
    """Routes to the first-run setup page until it's been completed, then
    the normal dashboard."""
    from app.core.onboarding import is_onboarding_complete
    path = "/ui/" if is_onboarding_complete() else "/ui/setup"
    return f"http://{settings.jarvis_host}:{settings.jarvis_port}{path}"


def _format_error_message(reason: str, correlation_id: str) -> str:
    from app.core.app_paths import logs_dir
    return (
        f"{reason}\n\n"
        f"Reference ID: {correlation_id}\n"
        f"Logs: {logs_dir()}"
    )


def _show_error_dialog(title: str, message: str) -> None:
    if sys.platform == "win32":
        import ctypes
        MB_OK_ICONERROR = 0x00000010
        ctypes.windll.user32.MessageBoxW(None, message, title, MB_OK_ICONERROR)


def _fail(reason: str) -> None:
    """One friendly native dialog carrying a correlation ID and the safe
    log *folder* (never a secret, never a raw traceback), then exit."""
    correlation_id = str(uuid.uuid4())
    logger.error("Launcher startup failed [%s]: %s", correlation_id, reason)
    _show_error_dialog("JARVIS couldn't start", _format_error_message(reason, correlation_id))
    sys.exit(1)


class LauncherSupervisor:
    """Owns both children and the lock. A class rather than module state
    so the whole lifecycle (start, restart, quit, server-failure
    recovery) is testable by constructing one with injected child
    supervisors."""

    def __init__(self, server=None, window=None) -> None:
        self._server = server
        self._window = window
        self._quitting = False

    # --- accessors used by the tray ---

    @property
    def server(self):
        return self._server

    @property
    def window(self):
        return self._window

    @property
    def quitting(self) -> bool:
        return self._quitting

    # --- startup ---

    def start_server(self) -> bool:
        """A fresh session secret per server start, by construction: a new
        ServerProcess generates its own."""
        self._server = server_process.ServerProcess(
            host=settings.jarvis_host, port=settings.jarvis_port,
        )
        try:
            self._server.start()
        except Exception:
            logger.error("Could not start the server child.", exc_info=True)
            return False
        return self._server.wait_until_healthy(timeout_seconds=SERVER_HEALTH_TIMEOUT_SECONDS)

    def start_window(self) -> bool:
        """Only ever called after the server has reported healthy."""
        self._window = window_process.WindowProcess(
            url=dashboard_url(), close_action=settings.jarvis_close_action,
        )
        return self._window.start()

    def open_or_focus_window(self) -> bool:
        """Tray "Open JARVIS": focus the live window, or start a fresh one
        if it crashed. Never creates a second."""
        if self._window is None:
            return self.start_window()
        return self._window.show_or_restart()

    # --- restart ---

    def restart(self) -> bool:
        """Window down, server down, fresh server, health, fresh window —
        in that order, so no child is ever left pointing at a runtime that
        is going away. The tray and the instance lock are untouched
        throughout: this process stays the authoritative owner."""
        logger.info("Restarting the JARVIS runtime.")
        if self._window is not None:
            self._window.stop(timeout_seconds=WINDOW_STOP_TIMEOUT_SECONDS)
            self._window = None
        if self._server is not None:
            self._server.stop(timeout_seconds=SERVER_STOP_TIMEOUT_SECONDS)
            self._server = None

        if not self.start_server():
            logger.error("Restart failed: the new server child never became healthy.")
            return False
        if not self.start_window():
            logger.error("Restart: server is healthy but the window child did not start.")
            return False
        return True

    # --- shutdown ---

    def quit(self) -> None:
        """Idempotent. Sets the quitting flag first so no recovery path
        can resurrect a child while shutdown is in progress."""
        if self._quitting:
            return
        self._quitting = True
        logger.info("JARVIS shutting down.")
        if self._window is not None:
            self._window.stop(timeout_seconds=WINDOW_STOP_TIMEOUT_SECONDS)
            self._window = None
        if self._server is not None:
            self._server.stop(timeout_seconds=SERVER_STOP_TIMEOUT_SECONDS)
            self._server = None
        instance_lock.release_lock()

    # --- failure detection ---

    def server_failed_unexpectedly(self) -> bool:
        """True when the server child died without us asking it to. The
        tray poll uses this to stop showing a healthy state."""
        if self._quitting or self._server is None:
            return False
        return not self._server.is_running()


def launch() -> LauncherSupervisor:
    """Full startup sequence. Exits the process via _fail() on any
    failure, after cleaning up whatever was already started."""
    boot_trace.trace("launch() starting")
    setup_logging()

    check = instance_lock.check_existing_instance(settings.jarvis_host, settings.jarvis_port)
    boot_trace.trace(
        f"check_existing_instance() -> another_running={check.another_instance_running} "
        f"port_conflict={check.port_in_use_by_other}"
    )
    if check.another_instance_running:
        # A second launch must not create anything. Opening the running
        # instance's dashboard is the closest this process can get to
        # "focus the existing window" without an IPC channel into a
        # parent it does not own.
        logger.info("JARVIS is already running (pid=%s) — opening its dashboard.", check.pid)
        webbrowser.open(dashboard_url())
        sys.exit(0)

    if check.port_in_use_by_other:
        _fail(
            f"Port {settings.jarvis_port} is already in use by another application "
            "(not JARVIS). Close that application, or change JARVIS_PORT, and try again."
        )

    instance_lock.acquire_lock()
    boot_trace.trace("lock acquired, starting server child")

    supervisor = LauncherSupervisor()
    if not supervisor.start_server():
        boot_trace.trace("server child never became healthy")
        supervisor.quit()  # releases the lock and stops whatever started
        _fail("JARVIS's local server did not start correctly.")

    boot_trace.trace("server healthy, starting window child")
    if not supervisor.start_window():
        # A missing window is degraded, not fatal: the server is healthy
        # and the tray still works, so fall back to the browser rather
        # than refusing to run.
        logger.warning("Window child unavailable — falling back to the browser.")
        boot_trace.trace("window child failed; browser fallback")
        webbrowser.open(dashboard_url())

    boot_trace.trace("launch() complete")
    return supervisor


def run_windowed() -> None:
    """The packaged entry point. The tray's message loop owns this
    process's main thread — the configuration that made graceful
    shutdown reliable — and never yields it to a GUI toolkit."""
    supervisor = launch()
    from app.launcher.tray import run_tray_loop

    try:
        run_tray_loop(supervisor, settings.jarvis_host, settings.jarvis_port)
    finally:
        supervisor.quit()
        boot_trace.trace("run_windowed() exiting")
