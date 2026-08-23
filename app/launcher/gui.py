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
from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.launcher import attention, boot_trace, desktop_ready, instance_lock, server_process, window_process
from app.logging_config import get_logger, setup_logging

logger = get_logger("launcher.gui")

SERVER_HEALTH_TIMEOUT_SECONDS = 30.0
SERVER_STOP_TIMEOUT_SECONDS = 8.0
WINDOW_STOP_TIMEOUT_SECONDS = 6.0
# Quit runs on tighter budgets than a restart. A restart can afford to
# wait for a child that is merely slow; someone who chose Quit is
# watching a tray icon that has not disappeared yet, and every second
# past the first few reads as "it is broken". Each stop() escalates
# quit -> terminate -> kill on its own budget, so the worst case is
# three times these numbers, and the tray arms a force-exit watchdog
# above even that.
QUIT_WINDOW_STOP_TIMEOUT_SECONDS = 4.0
QUIT_SERVER_STOP_TIMEOUT_SECONDS = 5.0
QUIT_PORT_RELEASE_TIMEOUT_SECONDS = 5.0


def dashboard_url() -> str:
    """Routes to the first-run setup page until it's been completed, then
    the normal dashboard."""
    from app.core.onboarding import is_onboarding_complete
    path = "/ui/" if is_onboarding_complete() else "/ui/setup"
    return f"http://{settings.jarvis_host}:{settings.jarvis_port}{path}"


def close_action() -> str:
    """What the window's X button does: "tray" or "quit".

    A saved preference wins over the environment variable, which supplies
    the starting default — the precedence rule preferences.py already
    documents. Before this, the only way to change it was an environment
    variable a packaged-app user does not have, behind a control on the
    setup screen that was wired to nothing.
    """
    from app.core.preferences import get as get_preference

    return get_preference("close_action") or settings.jarvis_close_action


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


@dataclass
class RestartResult:
    """The outcome of a restart, in enough detail to tell the user
    something true.

    `server_healthy` matters: a restart that brought the runtime back but
    could not reopen the window is a degraded success, not the failure
    the old single boolean reported it as.
    """

    ok: bool
    stage: str = ""
    window_reason: str = ""
    server_healthy: bool = False


class LauncherSupervisor:
    """Owns both children and the lock. A class rather than module state
    so the whole lifecycle (start, restart, quit, server-failure
    recovery) is testable by constructing one with injected child
    supervisors."""

    def __init__(self, server=None, window=None) -> None:
        self._server = server
        self._window = window
        self._quitting = False
        self._ready = None  # a DesktopReadyPublisher, once a server exists

    # --- readiness ---

    @property
    def ready_publisher(self):
        return self._ready

    def _new_ready_publisher(self):
        """One publisher per server child, because the secret it
        authenticates with belongs to that child. A restart therefore
        publishes under a new session_id by construction — which is what
        makes "the restart produced a genuinely fresh session" checkable
        rather than asserted."""
        if self._server is None:
            return None
        return desktop_ready.DesktopReadyPublisher(
            host=settings.jarvis_host,
            port=settings.jarvis_port,
            session_secret=self._server.session_secret,
            probe_window=self._probe_window,
        )

    def _probe_window(self) -> bool:
        """Real evidence: the window child answered a ping over the
        authenticated channel. `is_running()` would only prove a process
        exists, and a window whose command pump has died cannot be shown,
        restarted or quit from the tray."""
        window = self._window
        return bool(window is not None and window.responds_to_commands())

    def verify_window_ready(self) -> bool:
        """Re-probe the window and record the answer."""
        if self._ready is None:
            return self._probe_window()
        return self._ready.verify_window()

    def publish_readiness(self, **facts):
        """Record verified readiness facts. Safe to call before a server
        exists — startup does, and a dropped early update is not worth a
        crash."""
        if self._ready is None:
            return None
        return self._ready.update(**facts)

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
        healthy = self._server.wait_until_healthy(timeout_seconds=SERVER_HEALTH_TIMEOUT_SECONDS)
        # A fresh publisher per server child, and the first fact recorded
        # against it is the one this method just proved.
        self._ready = self._new_ready_publisher()
        self.publish_readiness(
            server_healthy=healthy,
            detail="Server running; starting the window." if healthy else "The server did not become healthy.",
        )
        return healthy

    def start_window(self) -> bool:
        return self.start_window_detailed().ok

    def start_window_detailed(self):
        """Only ever called after the server has reported healthy.

        Returns the typed result so callers can distinguish "the WebView2
        runtime is missing" from "something else went wrong" and offer the
        matching repair — a distinction the old boolean threw away.
        """
        self._window = window_process.WindowProcess(
            url=dashboard_url(), close_action=close_action(),
            # So the window child can authenticate the one thing it reports
            # to the server: which folder a person picked in a native dialog.
            session_secret=self._server.session_secret if self._server else "",
        )
        result = self._window.start_detailed()
        if not result.ok:
            # Do not keep a handle to a window that never opened: a later
            # stop() would otherwise spend its full timeout budget waiting
            # on a process that is already gone.
            self._window = None
        return result

    def open_or_focus_window(self) -> bool:
        """Tray "Open JARVIS": focus the live window, or start a fresh one
        if it crashed. Never creates a second."""
        return self.open_or_focus_window_detailed().ok

    def open_or_focus_window_detailed(self):
        """The same thing, reporting *why* it could not open a window.

        Every route a user takes to "open JARVIS" — the tray menu, the
        Start-menu shortcut of an already-running instance, the installer's
        finish action — ends here, and each of them needs to be able to
        explain a missing WebView2 runtime rather than quietly substituting
        a browser tab.

        A dead or unresponsive child is replaced rather than reused: a
        fresh WindowProcess picks up the current dashboard URL (which
        changes the moment onboarding completes) and closes the previous
        control channel instead of orphaning it.
        """
        if self._window is not None:
            if self._window.is_running() and self._window.show():
                return window_process.WindowStartResult(True, "")
            logger.info("The existing window child is gone or unresponsive — replacing it.")
            self._window.stop(timeout_seconds=WINDOW_STOP_TIMEOUT_SECONDS)
            self._window = None
        return self.start_window_detailed()

    # --- restart ---

    def restart(self) -> "RestartResult":
        """Window down, server down, port released, fresh server, health,
        fresh window — in that order, so no child is ever left pointing at
        a runtime that is going away, and the new server never races the
        old one's socket. The tray and the instance lock are untouched
        throughout: this process stays the authoritative owner.

        Reports which stage failed. The previous version returned one
        bare False for two very different outcomes — a dead runtime, and
        a perfectly healthy runtime whose window did not open — and told
        the user the same wrong thing ("the runtime did not come back up")
        either way.
        """
        logger.info("Restarting the JARVIS runtime.")
        if self._window is not None:
            self._window.stop(timeout_seconds=WINDOW_STOP_TIMEOUT_SECONDS)
            self._window = None

        released = True
        if self._server is not None:
            self._server.stop(timeout_seconds=SERVER_STOP_TIMEOUT_SECONDS)
            released = self._server.wait_until_port_released()
            self._server = None

        if not released:
            logger.error("Restart failed: the previous server did not release port %s.", settings.jarvis_port)
            return RestartResult(False, "port_busy")

        if not self.start_server():
            logger.error("Restart failed: the new server child never became healthy.")
            return RestartResult(False, "server_unhealthy")

        window = self.start_window_detailed()
        if not window.ok:
            # The runtime *did* come back. Saying otherwise would send the
            # user to quit and relaunch a working server.
            logger.error("Restart: server is healthy but the window did not open (%s).", window.reason)
            return RestartResult(False, "window_failed", window_reason=window.reason, server_healthy=True)
        return RestartResult(True, "")

    # --- shutdown ---

    def quit(self) -> None:
        """Full shutdown, in a deliberate order, on bounded budgets.

        Idempotent, and the quitting flag is set *first* so no recovery
        path can resurrect a child while shutdown is in progress.

        The order is the reason this works:

        1. **Window first.** It is the product's interface, so closing it
           is what actually stops new work being submitted. (The API is
           loopback-only; the only other client is this tray.) Stopping
           it also ends anything the page had running — speech capture
           and playback both live behind that page and the server child.
        2. **Server second**, which ends every remaining request, the
           speech runtime and any audio still playing with it.
        3. **Port**, verified released rather than assumed — otherwise
           the next launch meets its own dying predecessor and is told
           the port is "in use by another application".
        4. **Lock last**, so nothing can start a second instance while
           this one is still tearing down.

        Every step is bounded and none can block the next: a child that
        refuses to die is escalated to kill and then reported, never
        waited on indefinitely.
        """
        if self._quitting:
            return
        self._quitting = True
        logger.info("JARVIS shutting down.")
        if self._window is not None:
            self._window.stop(timeout_seconds=QUIT_WINDOW_STOP_TIMEOUT_SECONDS)
            self._window = None
        if self._server is not None:
            server = self._server
            self._server = None
            server.stop(timeout_seconds=QUIT_SERVER_STOP_TIMEOUT_SECONDS)
            if not server.wait_until_port_released(timeout_seconds=QUIT_PORT_RELEASE_TIMEOUT_SECONDS):
                logger.error(
                    "Port %s was still held at the end of shutdown; the next launch may "
                    "report it as in use.", settings.jarvis_port,
                )
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
        # A second launch must not create anything — and must not open a
        # browser either. Clicking the Start-menu shortcut while JARVIS is
        # already running is one of the ordinary ways a user "opens" the
        # app, and answering it with a browser tab is exactly the defect
        # that made the browser look like the product's real interface.
        # attention.request() leaves a signal the running instance's tray
        # loop picks up and turns into a real window focus.
        logger.info("JARVIS is already running (pid=%s) — asking it to show its window.", check.pid)
        attention.request()
        sys.exit(0)

    if check.port_in_use_by_other:
        _fail(
            f"Port {settings.jarvis_port} is already in use by another application "
            "(not JARVIS). Close that application, or change JARVIS_PORT, and try again."
        )

    instance_lock.acquire_lock()
    # A marker left by a crashed run must not make this one's window pop
    # up on its own a moment after startup.
    attention.clear()
    boot_trace.trace("lock acquired, starting server child")

    supervisor = LauncherSupervisor()
    if not supervisor.start_server():
        boot_trace.trace("server child never became healthy")
        supervisor.quit()  # releases the lock and stops whatever started
        _fail("JARVIS's local server did not start correctly.")

    boot_trace.trace("server healthy, starting window child")
    window = supervisor.start_window_detailed()
    # Proved by a real ping over the control channel, not by the start
    # having returned ok — see LauncherSupervisor._probe_window.
    supervisor.publish_readiness(
        window_alive=supervisor.verify_window_ready(),
        detail="Window open; starting the tray." if window.ok else f"The window did not open ({window.reason}).",
    )
    if not window.ok:
        # Deliberately NOT a silent browser fallback. Opening a browser
        # here hid a fixable problem and quietly made the browser the
        # product's interface — the owner rejected both. The user is told
        # what is missing, offered the fix, and left with a working tray
        # (including an explicit "Open in browser" if they want it now).
        logger.warning("Window child unavailable (%s) — telling the user.", window.reason)
        boot_trace.trace(f"window child failed: {window.reason}")
        _report_window_failure(window.reason)

    supervisor.publish_readiness(
        parent_running=True,
        detail="Startup complete; waiting for the tray message loop.",
    )
    boot_trace.trace("launch() complete")
    return supervisor


def _report_window_failure(reason: str) -> None:
    """One native dialog naming the missing runtime and offering the
    download page. Never a raw traceback, never a silent fallback."""
    from app.launcher import runtime_check

    correlation_id = str(uuid.uuid4())
    logger.error("Native window unavailable [%s]: %s", correlation_id, reason)

    explanation = runtime_check.describe(reason)
    fix_url = runtime_check.fix_url_for(reason)
    if fix_url:
        explanation += f"\n\nDownload it from:\n{fix_url}"
    explanation += (
        "\n\nJARVIS is running and its tray icon is available — use "
        "\"Open in browser\" there if you need it right now."
    )
    _show_error_dialog("JARVIS couldn't open its window", _format_error_message(explanation, correlation_id))


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
