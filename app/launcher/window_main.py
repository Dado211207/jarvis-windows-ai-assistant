"""Entry point for the window child process (`JARVIS.exe --window`).

This process does exactly one thing: own a pywebview window on its own
main thread. It never starts, owns, or supervises the server — it is
handed an already-healthy URL by the parent and simply displays it. If
this process dies, the server and tray are unaffected; the parent can
start a fresh one.

Why a separate process at all: pywebview must own its process's main
thread (webview/platforms/winforms.py calls signal.signal(), which
CPython only permits there). So does a reliable Win32 tray message loop.
Two things cannot both own one main thread, and when pywebview won that
contest inside the parent, graceful `taskkill` — the mechanism behind
Windows' "End task" and Inno Setup's CloseApplications=yes — stopped
being delivered reliably (one pass, two failures across three
consecutive runs on identical code; see docs/desktop-shell-findings.md).
Giving each its own process ends the contest instead of arbitrating it.

Launched without the inherited context an internal mode requires (a user
double-clicking a shortcut that somehow carries --window), this exits
cleanly with a short explanation rather than a traceback — see
main()'s context check.
"""

import sys
import threading
from typing import Optional

from app.launcher import ipc
from app.logging_config import get_logger, setup_logging

logger = get_logger("launcher.window_main")

COMMAND_POLL_INTERVAL_SECONDS = 0.2
NOT_A_USER_FACING_MODE_MESSAGE = (
    "This is an internal JARVIS component and is not meant to be started directly.\n"
    "Start JARVIS normally instead."
)


def _handle_command(command: str, window, close_action: str) -> bool:
    """Applies one validated command. Returns False when the loop should
    stop (a real quit). Split out from the polling loop so every command
    branch is testable against a fake window with no IPC at all."""
    from app.launcher import webview_window

    if command == ipc.COMMAND_QUIT:
        # A parent-initiated quit must never be converted into a
        # minimize-to-tray by the close_action setting — that is the
        # distinction the window has to draw between "the user clicked X"
        # and "the application is shutting down".
        webview_window.request_shutdown()
        try:
            window.destroy()
        except Exception:
            logger.warning("Window destroy failed during quit.", exc_info=True)
        return False
    if command == ipc.COMMAND_SHOW:
        window.show()
        window.restore()
    elif command == ipc.COMMAND_FOCUS:
        window.restore()
    elif command == ipc.COMMAND_HIDE:
        window.hide()
    elif command == ipc.COMMAND_RELOAD:
        try:
            window.load_url(window.get_current_url())
        except Exception:
            logger.warning("Window reload failed.", exc_info=True)
    return True


def _command_pump(client, window, close_action: str, stop_event: threading.Event) -> None:
    """Runs on a background thread of the *window* process: pywebview owns
    this process's main thread, so commands cannot be serviced there."""
    while not stop_event.is_set():
        command = client.poll_command(timeout=COMMAND_POLL_INTERVAL_SECONDS)
        if command is None:
            continue
        try:
            if not _handle_command(command, window, close_action):
                stop_event.set()
                return
        except Exception:
            logger.warning("Window command %r failed.", command, exc_info=True)


def main(argv=None, context=None, webview_module=None) -> int:
    """Returns a process exit code. *context* and *webview_module* are
    injection seams for tests; production passes neither."""
    setup_logging()

    context = context if context is not None else ipc.child_context_from_env()
    if context is None:
        # No inherited parent context: either a stale shortcut or someone
        # running an internal mode by hand. Say so plainly and exit 2
        # (distinct from a crash) rather than half-starting a window that
        # has no server behind it.
        logger.warning("--window started without a parent context; refusing to run.")
        print(NOT_A_USER_FACING_MODE_MESSAGE, file=sys.stderr)
        return 2

    try:
        client = ipc.ControlClient(context["address"], context["secret"])
    except Exception:
        logger.error("Window child could not authenticate to the parent.", exc_info=True)
        return 3

    from app.launcher import webview_window

    stop_event = threading.Event()
    close_reason = {"reason": ipc.REASON_USER_CLOSED}

    def _on_user_quit() -> None:
        """Fires only when the window actually closes for good (i.e. not a
        close-to-tray hide). If the parent asked for this, the reason was
        already set to quit_command by the command pump."""
        client.send_event(ipc.EVENT_CLOSED, reason=close_reason["reason"])
        stop_event.set()

    pump: Optional[threading.Thread] = None
    try:
        client.send_event(ipc.EVENT_READY)
        # The pump thread needs the window object, which only exists once
        # create_and_run() has built it — webview_window exposes it via
        # its module-level accessor, so the pump waits for it rather than
        # racing construction.
        def _start_pump_when_ready() -> None:
            for _ in range(100):
                window = webview_window.current_window()
                if window is not None:
                    _command_pump(client, window, context["close_action"], stop_event)
                    return
                if stop_event.wait(0.1):
                    return
            logger.warning("Window never became available for the command pump.")

        pump = threading.Thread(target=_start_pump_when_ready, daemon=True, name="jarvis-window-ipc")
        pump.start()

        webview_window.create_and_run(
            url=context["url"],
            icon_path=None,
            close_action=context["close_action"],
            on_quit=_on_user_quit,
            webview_module=webview_module,
        )
        return 0
    except Exception:
        logger.error("Window child failed to start its native window.", exc_info=True)
        client.send_event(ipc.EVENT_ERROR, detail="native window unavailable")
        return 4
    finally:
        stop_event.set()
        if pump is not None:
            pump.join(timeout=2)
        client.close()
