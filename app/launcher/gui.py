"""Pre-tray startup sequence for the packaged, windowed launcher.

launch() runs: single-instance check -> background server start ->
health-wait. It never prints to a console (none exists in a --windowed
PyInstaller build) and never lets a raw traceback reach the user — any
failure is logged with a correlation ID and shown as a native message
box naming the safe log location, then the process exits. On success it
returns the RunningServer; it deliberately does NOT itself open any UI
(browser or native window) any more — see run_windowed() below, which
decides how to present the UI once launch() has proven the server is
actually healthy. This split keeps launch() testable as a plain function
(call it, check what it returned/did) independent of any native GUI
toolkit.

run_windowed() is the real packaged entry point (run_jarvis.py calls it
by default): it runs the tray's own message loop on a background thread
and the native desktop window (app/launcher/webview_window.py) on the
main thread, since a native window's own event loop and a raw Win32
message loop are two independent per-thread things that don't share one
"main thread" requirement — see webview_window's module docstring for
the pywebview-specific reasoning. If the native window can't be created
for any reason, this falls back to opening the dashboard in the user's
default browser, so JARVIS keeps working either way.
"""

import sys
import threading
import uuid
import webbrowser
from typing import Optional

from app.config import settings
from app.launcher import boot_trace, instance_lock, server_runner
from app.logging_config import get_logger, setup_logging

logger = get_logger("launcher.gui")

TRAY_HWND_READY_TIMEOUT_SECONDS = 10.0


def dashboard_url() -> str:
    """Routes to the first-run setup page until it's been completed,
    then the normal dashboard — the packaged app's only entry point, so
    this is the one place that decides which a fresh install sees
    first. app/launcher/tray.py's "Open Command Center" deliberately
    does NOT go through this — it's a direct, explicit link to /ui/chat
    regardless of onboarding state."""
    from app.core.onboarding import is_onboarding_complete
    path = "/ui/" if is_onboarding_complete() else "/ui/setup"
    return f"http://{settings.jarvis_host}:{settings.jarvis_port}{path}"


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
    boot_trace.trace("launch() starting")
    setup_logging()
    boot_trace.trace("setup_logging() returned")

    check = instance_lock.check_existing_instance(settings.jarvis_host, settings.jarvis_port)
    boot_trace.trace(f"check_existing_instance() -> another_running={check.another_instance_running} port_conflict={check.port_in_use_by_other}")
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
    boot_trace.trace("lock acquired, starting server")
    running = server_runner.start_server_in_background()
    boot_trace.trace("start_server_in_background() returned, waiting for health")

    if not server_runner.wait_until_healthy():
        boot_trace.trace("wait_until_healthy() timed out")
        shutdown(running)
        _fail("JARVIS's local server did not become healthy in time.")
        return None  # unreachable

    boot_trace.trace("wait_until_healthy() succeeded")
    logger.info("JARVIS is healthy.")
    return running


def shutdown(running: server_runner.RunningServer) -> None:
    """Full graceful shutdown: stop uvicorn, release the single-instance
    lock. Idempotent enough to call once from the tray's Quit handler;
    not designed to be called concurrently from two threads."""
    logger.info("JARVIS shutting down.")
    running.request_shutdown()
    instance_lock.release_lock()


def _icon_path_for_window() -> Optional[str]:
    from pathlib import Path

    icon_path = Path(__file__).resolve().parent.parent / "ui" / "static" / "icon.ico"
    return str(icon_path) if icon_path.exists() else None


def _open_main_window(running: server_runner.RunningServer, request_tray_quit) -> None:
    """Presents the UI: a native desktop window if it can be created
    safely, the default browser otherwise. Blocks until the UI is closed
    (native window) or returns immediately (browser — nothing to block
    on). *request_tray_quit* is called when the window itself initiates
    a real quit (close_action="quit"), so the tray on its own thread
    tears down too instead of being left running with a dead server."""
    from app.launcher import webview_window

    if webview_window.is_supported():
        try:
            webview_window.create_and_run(
                url=dashboard_url(),
                icon_path=_icon_path_for_window(),
                close_action=settings.jarvis_close_action,
                on_quit=lambda: (shutdown(running), request_tray_quit()),
            )
            return
        except Exception:
            logger.warning(
                "Native window unavailable — opening the dashboard in the default "
                "browser instead.", exc_info=True,
            )
    webbrowser.open(dashboard_url())


def run_windowed() -> None:
    """The complete windowed entry point: launch() the server, run the
    tray's message loop on a background thread, then hand the main
    thread to the native window (or the browser fallback) until it's
    closed. This is what run_jarvis.py calls — the only place in this
    package that wires gui, tray, and webview_window together, kept out
    of all three modules' own import graphs so importing any one of them
    alone (as most of this package's tests do) never needs pywin32 or
    pywebview."""
    running = launch()
    from app.launcher.tray import run_tray_loop

    tray_hwnd: dict = {}
    tray_hwnd_ready = threading.Event()

    def _on_tray_hwnd_ready(hwnd: int) -> None:
        tray_hwnd["value"] = hwnd
        tray_hwnd_ready.set()

    tray_thread = threading.Thread(
        target=run_tray_loop,
        args=(running, settings.jarvis_host, settings.jarvis_port),
        kwargs={"on_hwnd_ready": _on_tray_hwnd_ready},
        daemon=False,
        name="jarvis-tray",
    )
    tray_thread.start()

    def _request_tray_quit() -> None:
        # Posting WM_CLOSE to the tray's own hidden window reuses its
        # existing do_quit() cleanup (icon removal, instance lock, etc.)
        # exactly the way a graceful taskkill already does — see
        # app/launcher/tray.py's wnd_proc(). A direct cross-thread call
        # into do_quit() would touch Win32 objects the tray thread owns
        # from the wrong thread; PostMessage is the correct, standard way
        # to hand control back to the thread that owns them.
        if not tray_hwnd_ready.wait(timeout=TRAY_HWND_READY_TIMEOUT_SECONDS):
            logger.warning("Tray window never signalled ready — shutting down directly instead.")
            shutdown(running)
            return
        hwnd = tray_hwnd.get("value")
        if hwnd is None:
            shutdown(running)
            return
        import win32con
        import win32gui
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)

    _open_main_window(running, _request_tray_quit)

    tray_thread.join()
