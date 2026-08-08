"""Native desktop window for the packaged launcher, via pywebview.

Windows-only in practice: pywebview's only Windows backend
(webview/platforms/winforms.py in pywebview 6.2.1, verified directly by
installing the real package and reading its source rather than assumed)
needs pythonnet + .NET WinForms — there is no lighter Windows backend to
pick instead. pythonnet has no non-Windows wheel, so every import of
`webview` here is kept strictly local to the functions that need it,
matching this package's established pattern for pywin32/keyring: this
module stays importable on Linux dev/CI, it just never constructs a real
window there. See requirements-windows.txt.

Deliberately forces gui="edgechromium" in start() and never lets
pywebview silently fall back to its legacy `mshtml` (IE/Trident) backend.
Verified against pywebview's own source (webview/guilib.py): on Windows,
pywebview always loads winforms.py, which itself decides between the
modern WebView2 (Chromium-based) control and the legacy Trident control
based on a registry probe for the Edge/WebView2 runtime — silently, with
no signal to the caller either way. An old Trident-engine window would be
a real security downgrade for an app whose first-run screen collects an
API key, so this module refuses to allow that trade silently: if
edgechromium can't be initialised for any reason (WebView2 Runtime
genuinely absent, pythonnet/.NET missing, or anything else),
create_and_run() raises and the caller (app/launcher/gui.py) falls back
to opening the dashboard in the user's default browser instead — the app
keeps working, just without the native window chrome, matching this
milestone's own explicit "open in browser" fallback requirement.
"""

import sys
import threading
from typing import Callable, Optional

from app.logging_config import get_logger

logger = get_logger("launcher.webview_window")

WINDOW_TITLE = "JARVIS"
MIN_WIDTH = 960
MIN_HEIGHT = 640
DEFAULT_WIDTH = 1180
DEFAULT_HEIGHT = 780
VALID_CLOSE_ACTIONS = ("tray", "quit")

_window = None  # the one window this process ever creates
_window_lock = threading.Lock()


def resolve_close_action(configured: str) -> str:
    """Normalises settings.jarvis_close_action. An unrecognised value
    falls back to "tray" — the safer default, since it can never
    surprise a user by silently ending a running server they didn't
    intend to stop."""
    value = (configured or "").strip().lower()
    return value if value in VALID_CLOSE_ACTIONS else "tray"


def is_supported() -> bool:
    """Best-effort platform check only — a real create_and_run() call is
    the actual test. Mirrors the "ready right now" style of check already
    used elsewhere in this codebase (e.g. app/voice/stt.py's
    model_status()) rather than assuming success from the platform alone."""
    return sys.platform == "win32"


def _make_handlers(close_action: str, on_quit: Callable[[], None]):
    """Returns (on_closing, on_closed) — split out from create_and_run()
    so tests/test_launcher_webview_window.py can exercise the actual
    close-to-tray-vs-quit decision logic without needing a real pywebview
    window: the *decision* here never touches a native object directly,
    only the module-level _window reference, which a test can substitute
    via an injected fake webview module in create_and_run()."""

    def on_closing() -> Optional[bool]:
        if close_action == "tray":
            logger.info("Window close requested — minimizing to tray (close_action=tray).")
            with _window_lock:
                if _window is not None:
                    _window.hide()
            return False  # pywebview's documented way to cancel a close (see webview/event.py)
        logger.info("Window close requested — quitting (close_action=quit).")
        return None  # let the close proceed

    def on_closed() -> None:
        global _window
        with _window_lock:
            _window = None
        if close_action == "quit":
            on_quit()

    return on_closing, on_closed


def create_and_run(
    url: str,
    icon_path: Optional[str],
    close_action: str,
    on_quit: Callable[[], None],
    webview_module=None,
) -> None:
    """Creates the window and blocks until it is destroyed.

    Must be called from the thread that should own the native GUI event
    loop — app/launcher/gui.py calls this from the process's main thread;
    the tray's own message loop runs on a background thread instead (see
    app/launcher/tray.py::run_tray_loop()'s docstring for why raw Win32
    message loops, unlike WinForms' own loop, don't need to be on the
    main thread).

    *webview_module* is a test-only injection point — production callers
    always leave it as None, which imports the real `webview` package.
    """
    global _window

    webview = webview_module
    if webview is None:
        import webview as _real_webview
        webview = _real_webview

    close_action = resolve_close_action(close_action)
    on_closing, on_closed = _make_handlers(close_action, on_quit)

    window = webview.create_window(
        WINDOW_TITLE,
        url=url,
        width=DEFAULT_WIDTH,
        height=DEFAULT_HEIGHT,
        min_size=(MIN_WIDTH, MIN_HEIGHT),
        resizable=True,
        confirm_close=False,  # closing is handled explicitly by the events below instead
        text_select=True,
    )
    window.events.closing += on_closing
    window.events.closed += on_closed

    with _window_lock:
        _window = window

    logger.info("Starting native window (close_action=%s).", close_action)
    webview.start(gui="edgechromium", icon=icon_path, debug=False)


def show_existing() -> bool:
    """Brings the existing window to the front, un-hiding it if it was
    previously closed-to-tray. Returns False if no window exists yet (it
    was never created, or the process is running in browser-fallback
    mode) — the caller should fall back to another way of surfacing the
    UI in that case, not assume this always succeeds."""
    with _window_lock:
        window = _window
    if window is None:
        return False
    try:
        window.show()
        window.restore()
        return True
    except Exception:
        logger.warning("Could not restore the existing window.", exc_info=True)
        return False


def destroy_existing() -> None:
    """Best-effort teardown of the native window from outside its own
    thread — used by the tray's Quit path so a tray-initiated quit also
    ends the window (and, via its own on_closed handler, the rest of
    clean shutdown). Safe to call when no window exists (browser-fallback
    mode, or the window was already closed)."""
    with _window_lock:
        window = _window
    if window is None:
        return
    try:
        window.destroy()
    except Exception:
        logger.warning("Could not destroy the existing window.", exc_info=True)
