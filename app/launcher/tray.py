"""System tray control — replaces a visible console/window as the only
way to see JARVIS is running and to stop it, restart it, or jump to the
dashboard, once closing the browser tab would otherwise leave a
headless background process with no visible control surface.

Built on pystray, which probes for and selects its native backend
(Win32/AppKit/GTK/Xorg) at *import* time. That import is kept strictly
local to run_tray_loop() — the one function in this module that actually
builds and runs a real tray icon — so merely importing this module (as
its own test file, and any future caller inspecting its pure helpers,
does) never touches a display backend. This matters concretely: it's
what keeps this module safely importable on this repo's own headless
Linux CI, and on any Windows CI runner without an interactive desktop
session, neither of which can construct a real system tray icon at all.

Honesty note, matching this codebase's existing "say what's actually
verified" standard for push-to-talk: build_menu_entries(), the label
functions, and TrayApiClient (tested in tests/test_launcher_tray.py and
tests/test_launcher_tray_client.py) are real, exercised logic. The
native pystray event loop itself (run_tray_loop()'s call to icon.run())
is not exercised by any automated test in this repository — there is no
headless-tray-testing story pystray itself offers, and CI has no
interactive desktop session to render a real tray icon into. That is a
real gap, not a silently-assumed one; see the packaging report.
"""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from app.launcher import gui, server_runner
from app.launcher.server_runner import RunningServer
from app.launcher.tray_client import TrayApiClient
from app.logging_config import get_logger

logger = get_logger("launcher.tray")

POLL_INTERVAL_SECONDS = 5.0
ICON_ASSET_PATH = Path(__file__).resolve().parent.parent / "ui" / "static" / "icon.png"


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
    on_toggle_privacy: Callable[[], None],
    on_restart: Callable[[], None],
    on_quit: Callable[[], None],
) -> List[MenuEntry]:
    """Pure data describing the menu — no pystray objects. This is what
    tests/test_launcher_tray.py exercises directly; run_tray_loop() is
    the only place this gets turned into a real pystray.Menu."""
    return [
        MenuEntry(status_label(state), None, enabled=False),
        MenuEntry("Open JARVIS", on_open_dashboard),
        MenuEntry("Open Command Center", on_open_command_center),
        MenuEntry(privacy_label(state), on_toggle_privacy, enabled=state.privacy_active is not None),
        MenuEntry("Restart JARVIS", on_restart),
        MenuEntry("Quit JARVIS", on_quit),
    ]


def load_icon_image():
    """Returns a PIL.Image — the real bundled icon if present (see
    app/ui/static/icon.png, added once task 34's icon asset lands), else
    a small generated placeholder so this function (and everything that
    calls it) works today and stays safe if a packaging bug ever drops
    the asset. PIL/Pillow is always safe to import (unlike pystray) —
    it does no display-backend probing at import time."""
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


def _poll_loop(client: TrayApiClient, state: TrayState, icon, stop_event: threading.Event) -> None:
    while not stop_event.wait(POLL_INTERVAL_SECONDS):
        state.status = "running" if client.is_healthy() else "offline"
        state.privacy_active = client.privacy_active()
        try:
            icon.update_menu()
        except Exception:
            pass  # icon may already be tearing down


def run_tray_loop(running: RunningServer, host: str, port: int) -> None:
    """Blocks until Quit is chosen. The only function in this module
    that imports pystray or constructs real pystray objects."""
    import pystray
    import webbrowser

    client = TrayApiClient(host, port)
    state = TrayState()
    stop_event = threading.Event()

    def open_dashboard() -> None:
        webbrowser.open(gui.dashboard_url())

    def open_command_center() -> None:
        webbrowser.open(f"http://{host}:{port}/ui/chat")

    def toggle_privacy() -> None:
        if state.privacy_active is None:
            return
        client.set_privacy_mode(not state.privacy_active)
        state.privacy_active = client.privacy_active()

    def do_restart() -> None:
        nonlocal running
        logger.info("Tray: restarting JARVIS.")
        running.request_shutdown()
        running = server_runner.start_server_in_background(host=host, port=port)
        server_runner.wait_until_healthy(host=host, port=port)

    def do_quit() -> None:
        logger.info("Tray: quitting JARVIS.")
        stop_event.set()
        gui.shutdown(running)  # stops uvicorn (whose own shutdown releases TTS/voice resources) and the instance lock
        client.close()
        icon.stop()

    icon = pystray.Icon(
        "JARVIS",
        icon=load_icon_image(),
        title="JARVIS",
        menu=pystray.Menu(
            pystray.MenuItem(lambda item: status_label(state), None, enabled=False),
            pystray.MenuItem("Open JARVIS", open_dashboard),
            pystray.MenuItem("Open Command Center", open_command_center),
            pystray.MenuItem(
                lambda item: privacy_label(state),
                toggle_privacy,
                enabled=lambda item: state.privacy_active is not None,
            ),
            pystray.MenuItem("Restart JARVIS", do_restart),
            pystray.MenuItem("Quit JARVIS", do_quit),
        ),
    )

    poll_thread = threading.Thread(target=_poll_loop, args=(client, state, icon, stop_event), daemon=True)
    poll_thread.start()
    icon.run()
