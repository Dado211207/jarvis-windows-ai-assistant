"""System tray control — replaces a visible console/window as the only
way to see JARVIS is running and to stop it, restart it, or jump to the
dashboard, once closing the browser tab would otherwise leave a
headless background process with no visible control surface.

Native pywin32 implementation (Shell_NotifyIcon + a hidden message
window + a real Win32 popup menu), not pystray. Why: pystray's own
license is LGPL-3.0, and PyInstaller-bundling an LGPL-licensed library
into a single frozen onedir distribution raises a real, unresolved
question about LGPLv3 Section 4's "user must be able to relink a
modified copy of the library" obligation — a legal compliance
conclusion this project is not in a position to assert confidently
either way. Rather than ship that uncertainty, this sidesteps it
entirely: pywin32 is PSF-2.0 (permissive, no copyleft, no
relink/disclosure obligation regardless of how it's bundled), is
Windows-only (matching an app that only ever ships a Windows tray),
and is far more actively maintained (a release within the last two
months at the time of writing, vs. pystray's ~2-year-old last release).
See docs/THIRD_PARTY_NOTICES.md for the full comparison and reasoning.

pywin32 ships native C-extension modules built per Windows Python
version; it has no Linux/macOS wheel at all, so — like pystray before
it — every pywin32 import is kept strictly local to run_tray_loop(),
the one function that actually builds and runs a real tray icon. That
keeps this module (and its own pure logic below, which is what
tests/test_launcher_tray.py exercises) safely importable on this repo's
headless Linux CI and on any Windows CI runner without an interactive
desktop session — neither can construct a real tray icon at all.

Honesty note, matching this codebase's "say what's actually verified"
standard for push-to-talk: build_menu_entries(), the label functions,
load_icon_image(), _poll_loop(), _attention_loop() and the two failure
dialogs are real, exercised logic. The native Win32
window/message-loop/menu code in run_tray_loop() itself has no automated
test coverage — it could not even be import-checked in this project's
Linux development sandbox, since pywin32 has nothing to install there.
Real verification is the manual Windows acceptance test; see the
packaging report.
"""

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from app.launcher import attention, gui
from app.launcher.tray_client import TrayApiClient
from app.logging_config import get_logger

logger = get_logger("launcher.tray")

POLL_INTERVAL_SECONDS = 5.0
# Deliberately much finer than the health poll. A status dot can be five
# seconds stale without anyone noticing; someone who just double-clicked
# the Start-menu shortcut is watching for a window right now.
ATTENTION_POLL_INTERVAL_SECONDS = 0.5
# The backstop behind Quit. Must exceed the worst case of every bounded
# shutdown step below it (see gui.LauncherSupervisor.quit) — verified by
# test_the_force_exit_backstop_cannot_cut_a_working_shutdown_short.
QUIT_FORCE_EXIT_SECONDS = 45.0
ICON_ASSET_PATH = Path(__file__).resolve().parent.parent / "ui" / "static" / "icon.ico"


@dataclass
class TrayState:
    status: str = "starting"  # "running" | "offline" | "starting"
    privacy_active: Optional[bool] = None  # None = unknown (server unreachable)


@dataclass
class MenuEntry:
    label: str
    action: Optional[Callable[[], None]]
    enabled: bool = True


def status_label(state: TrayState) -> str:
    return f"Status: {state.status}"


def privacy_label(state: TrayState) -> str:
    if state.privacy_active is True:
        return "Privacy mode: ON"
    if state.privacy_active is False:
        return "Privacy mode: OFF"
    return "Privacy mode: unknown"


def build_menu_entries(
    state: TrayState,
    on_open_dashboard: Callable[[], None],
    on_open_command_center: Callable[[], None],
    on_open_in_browser: Callable[[], None],
    on_toggle_privacy: Callable[[], None],
    on_restart: Callable[[], None],
    on_quit: Callable[[], None],
) -> List[MenuEntry]:
    """Pure data describing the menu — no Win32 objects. This is what
    tests/test_launcher_tray.py exercises directly; run_tray_loop() is
    the only place this gets turned into a real native popup menu, built
    fresh from current state on every right-click (see show_context_menu()
    inside run_tray_loop() — a native popup menu has no persistent object
    to keep in sync the way a cross-platform library's Menu would).

    "Open JARVIS" brings the native desktop window to front, starting a
    fresh one if the previous window child died — never a browser, and
    never a second window (see LauncherSupervisor.open_or_focus_window).
    "Open in Browser" is the *explicitly named* browser action, the only
    place in the product where a browser tab is offered: always
    available, for anyone who prefers one or whose window cannot open
    right now."""
    return [
        MenuEntry(status_label(state), None, enabled=False),
        MenuEntry("Open JARVIS", on_open_dashboard),
        MenuEntry("Open in Browser", on_open_in_browser),
        MenuEntry("Open Command Center", on_open_command_center),
        MenuEntry(privacy_label(state), on_toggle_privacy, enabled=state.privacy_active is not None),
        MenuEntry("Restart JARVIS", on_restart),
        MenuEntry("Quit JARVIS", on_quit),
    ]


def load_icon_image():
    """Returns a PIL.Image — the real bundled icon if present (see
    app/ui/static/icon.ico, added by task 34's icon asset), else a small
    generated placeholder so this function (and everything that calls
    it) works today and stays safe if a packaging bug ever drops the
    asset. PIL/Pillow is always safe to import (unlike pywin32) — it
    does no native-backend probing at import time."""
    from PIL import Image, ImageDraw

    if ICON_ASSET_PATH.exists():
        try:
            return Image.open(ICON_ASSET_PATH)
        except Exception:
            logger.warning("Could not load icon asset %s — using placeholder.", ICON_ASSET_PATH, exc_info=True)

    size = 64
    image = Image.new("RGBA", (size, size), (15, 23, 42, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((3, 3, size - 3, size - 3), fill=(56, 189, 248, 255))
    draw.text((size // 2 - 6, size // 2 - 11), "J", fill=(15, 23, 42, 255))
    return image


def _resolve_icon_file() -> Path:
    """A real filesystem path to hand to Win32's LoadImage, which (unlike
    pystray) takes a file path rather than an in-memory image — the
    bundled .ico if present, otherwise load_icon_image()'s placeholder
    written out to a temp file."""
    if ICON_ASSET_PATH.exists():
        return ICON_ASSET_PATH

    import tempfile
    fd, tmp_path = tempfile.mkstemp(suffix=".ico")
    import os
    os.close(fd)
    load_icon_image().save(tmp_path, format="ICO")
    return Path(tmp_path)


def _poll_loop(client: TrayApiClient, state: TrayState, icon, stop_event: threading.Event) -> None:
    """*icon* only needs an update_menu() method — run_tray_loop() below
    passes a thin adapter around Shell_NotifyIcon's tooltip update, since
    a native popup menu (unlike pystray's Menu object) has nothing
    persistent to refresh; only the tray icon's hover tooltip benefits
    from a periodic push. Kept as its own function, taking a duck-typed
    icon, so this loop's logic is testable without any native library."""
    while not stop_event.wait(POLL_INTERVAL_SECONDS):
        state.status = "running" if client.is_healthy() else "offline"
        state.privacy_active = client.privacy_active()
        try:
            icon.update_menu()
        except Exception:
            pass  # icon may already be tearing down


def _attention_loop(on_attention: Callable[[], None], stop_event: threading.Event) -> None:
    """Turns a second launch's "show yourself" marker into a real window.

    Clicking the Start-menu shortcut, the desktop shortcut or the
    installer's finish action while JARVIS is already running used to be
    answered with a browser tab — which is how the browser came to look
    like the product's real interface. The second process exits
    immediately after leaving a marker (see app/launcher/attention.py);
    this loop is the running instance's side of that handshake.

    Its own loop rather than a branch in _poll_loop() because the two have
    genuinely different deadlines, and never raises: a marker that cannot
    be turned into a window must not take the poll thread down with it.
    """
    while not stop_event.wait(ATTENTION_POLL_INTERVAL_SECONDS):
        try:
            if attention.consume():
                logger.info("Another launch asked JARVIS to show its window.")
                on_attention()
        except Exception:
            logger.warning("Could not show the window on request.", exc_info=True)


_RESTART_STAGE_MESSAGES = {
    "port_busy": (
        "JARVIS's previous session was still holding its network port, so the "
        "new one could not start.\n\n"
        "This usually clears on its own. Use Quit, wait a few seconds, and start "
        "JARVIS again."
    ),
    "server_unhealthy": (
        "JARVIS's local service did not come back up after the restart.\n\n"
        "Use Quit and start JARVIS again."
    ),
}


def _show_restart_failure(stage: str = "") -> None:
    """An honest, native failure notice when Restart could not bring the
    runtime back — silently leaving a dead tray icon would be worse.

    The message names the stage that actually failed. Every restart
    failure used to produce the same sentence regardless of cause, which
    made the one real report of this defect impossible to act on: "the
    runtime did not come back up" is simply untrue when the runtime is
    healthy and only the window failed.
    """
    reason = _RESTART_STAGE_MESSAGES.get(
        stage,
        "JARVIS's runtime did not come back up after a restart.\n"
        "Use Quit and start JARVIS again.",
    )
    gui._show_error_dialog(
        "JARVIS couldn't restart",
        gui._format_error_message(reason, uuid.uuid4().hex),
    )


def _show_window_failure(window_reason: str) -> None:
    """The runtime is fine; only the window did not open. Names the
    missing runtime and offers the fix, rather than blaming the restart."""
    from app.launcher import runtime_check

    explanation = runtime_check.describe(window_reason)
    fix_url = runtime_check.fix_url_for(window_reason)
    if fix_url:
        explanation += f"\n\nDownload it from:\n{fix_url}"
    explanation += (
        "\n\nJARVIS itself is running normally — use \"Open in browser\" from "
        "this tray menu in the meantime."
    )
    gui._show_error_dialog(
        "JARVIS couldn't open its window",
        gui._format_error_message(explanation, uuid.uuid4().hex),
    )


def run_tray_loop(
    supervisor,
    host: str,
    port: int,
    on_hwnd_ready: Optional[Callable[[int], None]] = None,
) -> None:
    """Blocks until Quit is chosen. The only function in this module
    that imports pywin32 or constructs real Win32 objects.

    Runs on the parent process's MAIN thread — see
    app/launcher/gui.py's module docstring for why that matters for
    graceful shutdown. *supervisor* is the LauncherSupervisor owning the
    server and window children; this loop never touches a child process
    directly, it only asks the supervisor to act.

    *on_hwnd_ready*, if given, is called once with the tray's hidden
    window handle as soon as it exists."""
    import win32api
    import win32con
    import win32gui

    client = TrayApiClient(host, port)
    state = TrayState()
    stop_event = threading.Event()

    WM_TRAYICON = win32con.WM_USER + 20
    ICON_UID = 1

    def open_dashboard() -> None:
        # Focus the live window child, or start a fresh one if it crashed
        # — never a second window. Deliberately no silent browser
        # fallback: answering "open JARVIS" with a browser tab is what
        # made the browser look like the product's real interface. If the
        # window cannot open, say what is missing and point at the
        # explicitly named "Open in Browser" entry right above.
        result = supervisor.open_or_focus_window_detailed()
        if not result.ok:
            _show_window_failure(result.reason)

    def open_in_browser() -> None:
        # Explicit, always-available fallback — independent of whether
        # the native window exists, per this milestone's own requirement
        # that a browser tab always remains an option, not just an
        # emergency degrade path.
        import webbrowser
        webbrowser.open(gui.dashboard_url())

    def open_command_center() -> None:
        import webbrowser
        webbrowser.open(f"http://{host}:{port}/ui/chat")

    def toggle_privacy() -> None:
        if state.privacy_active is None:
            return
        client.set_privacy_mode(not state.privacy_active)
        state.privacy_active = client.privacy_active()

    def do_restart() -> None:
        """Window down, server down, port released, fresh server, health,
        fresh window — the supervisor owns that ordering. The tray process
        and the instance lock are deliberately untouched: this parent
        stays the authoritative owner across a restart."""
        logger.info("Tray: restarting JARVIS.")
        state.status = "starting"
        result = supervisor.restart()
        if result.ok:
            return

        logger.error("Tray: restart failed at stage %r.", result.stage)
        if result.server_healthy:
            # The runtime came back; only the window did not open. Telling
            # the user to quit and relaunch a healthy server — which is
            # what the old single failure message did — sends them to fix
            # something that is not broken.
            state.status = "running"
            _show_window_failure(result.window_reason)
            return
        state.status = "offline"
        _show_restart_failure(result.stage)

    quit_started = threading.Event()

    def do_quit() -> None:
        if quit_started.is_set():
            return  # a menu-driven Quit and a WM_CLOSE could both reach here
        from app.launcher import boot_trace, webview_window
        boot_trace.trace("tray do_quit() starting")
        quit_started.set()
        logger.info("Tray: quitting JARVIS.")
        # Armed first, and deliberately never cancelled: once Quit has
        # been chosen, this process ends. Everything below is individually
        # bounded, so this should never fire — but "should never" is not a
        # guarantee, and a desktop app that cannot be closed is a worse
        # outcome than one that exits abruptly. The grace period is set
        # above the sum of every bounded step so a slow-but-working
        # shutdown is never cut short.
        webview_window.force_exit_after(QUIT_FORCE_EXIT_SECONDS)
        stop_event.set()
        # supervisor.quit() sets its own quitting flag first, so no
        # recovery path can resurrect a child mid-shutdown, then stops the
        # window child, stops the server child, and releases the instance
        # lock — each with a bounded timeout and escalation, and only
        # against processes this launcher itself started.
        supervisor.quit()
        boot_trace.trace("tray do_quit() children stopped and lock released")
        client.close()
        win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, ICON_UID))
        win32gui.DestroyWindow(hwnd)  # synchronously delivers WM_DESTROY, which posts WM_QUIT below
        boot_trace.trace("tray do_quit() finished")

    def show_context_menu() -> None:
        # Built fresh from current state on every click — a native popup
        # menu has no persistent object to keep synchronized the way a
        # cross-platform library's Menu would.
        entries = build_menu_entries(
            state,
            on_open_dashboard=open_dashboard,
            on_open_command_center=open_command_center,
            on_open_in_browser=open_in_browser,
            on_toggle_privacy=toggle_privacy,
            on_restart=do_restart,
            on_quit=do_quit,
        )
        menu = win32gui.CreatePopupMenu()
        action_by_id = {}
        for i, entry in enumerate(entries):
            command_id = 1000 + i
            flags = win32con.MF_STRING
            if entry.action is None or not entry.enabled:
                flags |= win32con.MF_GRAYED
            win32gui.AppendMenu(menu, flags, command_id, entry.label)
            if entry.action is not None:
                action_by_id[command_id] = entry.action

        pos = win32gui.GetCursorPos()
        # Standard Win32 tray-menu trick: a hidden window must be the
        # foreground window while the popup is tracked, or the menu
        # silently fails to dismiss on an outside click.
        win32gui.SetForegroundWindow(hwnd)
        selected = win32gui.TrackPopupMenu(
            menu, win32con.TPM_LEFTALIGN | win32con.TPM_RETURNCMD, pos[0], pos[1], 0, hwnd, None,
        )
        win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)
        if selected in action_by_id:
            action_by_id[selected]()

    # Diagnostic only: which window messages does this hidden window
    # actually receive? Added after a real windows-latest CI failure
    # where a graceful `taskkill` (no /F) produced *no* trace from either
    # this wnd_proc or the native window's own closing handler — the
    # process simply ignored the request. Tracing a small allowlist of
    # shutdown-related messages (not every message, which would flood the
    # trace file) is what distinguishes "the message never arrived" from
    # "it arrived and the handler misbehaved" — two very different bugs
    # that look identical from the outside.
    _SHUTDOWN_MESSAGES = {
        win32con.WM_CLOSE: "WM_CLOSE",
        win32con.WM_DESTROY: "WM_DESTROY",
        win32con.WM_QUIT: "WM_QUIT",
        win32con.WM_QUERYENDSESSION: "WM_QUERYENDSESSION",
        win32con.WM_ENDSESSION: "WM_ENDSESSION",
    }

    def wnd_proc(hwnd_, msg, wparam, lparam):
        if msg in _SHUTDOWN_MESSAGES:
            from app.launcher import boot_trace
            boot_trace.trace(f"tray wnd_proc received {_SHUTDOWN_MESSAGES[msg]}")
        if msg == WM_TRAYICON and lparam in (win32con.WM_LBUTTONUP, win32con.WM_RBUTTONUP):
            show_context_menu()
            return 0
        if msg == win32con.WM_QUERYENDSESSION:
            # Windows logoff/shutdown asks permission first; answering
            # non-zero (allow) and treating it as a real close request is
            # what lets JARVIS exit cleanly on a session end instead of
            # being killed outright.
            do_quit()
            return 1
        if msg == win32con.WM_CLOSE:
            # A graceful `taskkill` (no /F) and Inno Setup's
            # CloseApplicationsFilter (Restart Manager close request)
            # both deliver WM_CLOSE, not WM_DESTROY — without this handler
            # neither would ever reach do_quit()'s cleanup.
            do_quit()
            return 0
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd_, msg, wparam, lparam)

    window_class = win32gui.WNDCLASS()
    window_class.hInstance = win32api.GetModuleHandle(None)
    window_class.lpszClassName = "JarvisTrayWindow"
    window_class.lpfnWndProc = wnd_proc
    class_atom = win32gui.RegisterClass(window_class)

    # Never shown (no WS_VISIBLE, never ShowWindow'd) — its only purpose
    # is to own the tray icon and receive its click messages.
    hwnd = win32gui.CreateWindow(
        class_atom, "JARVIS", 0, 0, 0, 0, 0, 0, 0, window_class.hInstance, None,
    )

    if on_hwnd_ready is not None:
        on_hwnd_ready(hwnd)

    icon_path = str(_resolve_icon_file())
    hicon = win32gui.LoadImage(
        0, icon_path, win32con.IMAGE_ICON, 0, 0, win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
    )

    win32gui.Shell_NotifyIcon(
        win32gui.NIM_ADD,
        (hwnd, ICON_UID, win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP, WM_TRAYICON, hicon, "JARVIS"),
    )

    class _TooltipHandle:
        """Adapter matching _poll_loop()'s icon.update_menu() contract —
        refreshes the tray icon's hover tooltip, the only thing worth
        pushing periodically (the menu itself is always built fresh; see
        show_context_menu())."""
        def update_menu(self) -> None:
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_MODIFY,
                (hwnd, ICON_UID, win32gui.NIF_TIP, WM_TRAYICON, hicon, status_label(state)),
            )

    poll_thread = threading.Thread(
        target=_poll_loop, args=(client, state, _TooltipHandle(), stop_event), daemon=True,
    )
    poll_thread.start()

    attention_thread = threading.Thread(
        target=_attention_loop, args=(open_dashboard, stop_event), daemon=True,
        name="jarvis-tray-attention",
    )
    attention_thread.start()

    from app.launcher import boot_trace
    boot_trace.trace("tray PumpMessages() starting")
    win32gui.PumpMessages()
    boot_trace.trace("tray PumpMessages() returned")
