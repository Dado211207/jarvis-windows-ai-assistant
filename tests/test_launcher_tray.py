"""Tests for the pure, display-independent logic in app/launcher/tray.py:
label formatting, menu-entry construction, icon loading, and the poll
loop's state updates. Deliberately never imports pystray or calls
run_tray_loop() — see tray.py's module docstring for why: pystray probes
for a real display backend at import time, which this repo's own
headless Linux CI (and some Windows CI runners without an interactive
desktop) cannot provide.
"""

import threading
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Label formatting
# ---------------------------------------------------------------------------

def test_status_label_reflects_state():
    from app.launcher.tray import TrayState, status_label
    assert status_label(TrayState(status="running")) == "Status: running"
    assert status_label(TrayState(status="offline")) == "Status: offline"


def test_privacy_label_on_off_unknown():
    from app.launcher.tray import TrayState, privacy_label
    assert privacy_label(TrayState(privacy_active=True)) == "Privacy mode: ON"
    assert privacy_label(TrayState(privacy_active=False)) == "Privacy mode: OFF"
    assert privacy_label(TrayState(privacy_active=None)) == "Privacy mode: unknown"


# ---------------------------------------------------------------------------
# build_menu_entries
# ---------------------------------------------------------------------------

def test_build_menu_entries_shape_and_order():
    from app.launcher.tray import TrayState, build_menu_entries

    calls = []
    state = TrayState(status="running", privacy_active=False)
    entries = build_menu_entries(
        state,
        on_open_dashboard=lambda: calls.append("dashboard"),
        on_open_command_center=lambda: calls.append("command_center"),
        on_toggle_privacy=lambda: calls.append("privacy"),
        on_restart=lambda: calls.append("restart"),
        on_quit=lambda: calls.append("quit"),
    )

    labels = [e.label for e in entries]
    assert labels == [
        "Status: running",
        "Open JARVIS",
        "Open Command Center",
        "Privacy mode: OFF",
        "Restart JARVIS",
        "Quit JARVIS",
    ]
    # Status entry is a display-only label — no action, disabled.
    assert entries[0].action is None
    assert entries[0].enabled is False

    # Every action-bearing entry actually invokes the callback it was given.
    for entry, expected in zip(entries[1:], ["dashboard", "command_center", "privacy", "restart", "quit"]):
        entry.action()
    assert calls == ["dashboard", "command_center", "privacy", "restart", "quit"]


def test_privacy_entry_disabled_when_state_unknown():
    from app.launcher.tray import TrayState, build_menu_entries

    entries = build_menu_entries(
        TrayState(privacy_active=None),
        on_open_dashboard=lambda: None,
        on_open_command_center=lambda: None,
        on_toggle_privacy=lambda: None,
        on_restart=lambda: None,
        on_quit=lambda: None,
    )
    privacy_entry = next(e for e in entries if e.label.startswith("Privacy mode"))
    assert privacy_entry.enabled is False


# ---------------------------------------------------------------------------
# load_icon_image
# ---------------------------------------------------------------------------

def test_load_icon_image_returns_a_valid_image(monkeypatch):
    from app.launcher import tray
    # Force the "no bundled asset yet" path regardless of what's actually
    # on disk in this environment.
    monkeypatch.setattr(tray, "ICON_ASSET_PATH", tray.ICON_ASSET_PATH.parent / "definitely-does-not-exist.png")

    image = tray.load_icon_image()

    assert image.size == (64, 64)


def test_load_icon_image_uses_real_asset_when_present(tmp_path, monkeypatch):
    from PIL import Image
    from app.launcher import tray

    asset_path = tmp_path / "icon.png"
    Image.new("RGBA", (32, 32), (1, 2, 3, 255)).save(asset_path)
    monkeypatch.setattr(tray, "ICON_ASSET_PATH", asset_path)

    image = tray.load_icon_image()

    assert image.size == (32, 32)


# ---------------------------------------------------------------------------
# _poll_loop
# ---------------------------------------------------------------------------

def test_poll_loop_updates_state_and_refreshes_icon_once_per_tick():
    from app.launcher.tray import TrayState, _poll_loop

    state = TrayState()
    client = MagicMock(is_healthy=MagicMock(return_value=True), privacy_active=MagicMock(return_value=True))
    icon = MagicMock()

    # A stop_event whose .wait() returns True (meaning "stop requested")
    # on the second call lets the loop body run exactly once before exiting.
    stop_event = MagicMock(spec=threading.Event)
    stop_event.wait.side_effect = [False, True]

    _poll_loop(client, state, icon, stop_event)

    assert state.status == "running"
    assert state.privacy_active is True
    icon.update_menu.assert_called_once()


def test_poll_loop_marks_offline_when_health_check_fails():
    from app.launcher.tray import TrayState, _poll_loop

    state = TrayState(status="running")
    client = MagicMock(is_healthy=MagicMock(return_value=False), privacy_active=MagicMock(return_value=None))
    icon = MagicMock()
    stop_event = MagicMock(spec=threading.Event)
    stop_event.wait.side_effect = [False, True]

    _poll_loop(client, state, icon, stop_event)

    assert state.status == "offline"


def test_poll_loop_survives_icon_update_menu_raising():
    """The icon can be mid-teardown when a poll tick lands — must not
    crash the poll thread."""
    from app.launcher.tray import TrayState, _poll_loop

    state = TrayState()
    client = MagicMock(is_healthy=MagicMock(return_value=True), privacy_active=MagicMock(return_value=False))
    icon = MagicMock()
    icon.update_menu.side_effect = RuntimeError("icon is gone")
    stop_event = MagicMock(spec=threading.Event)
    stop_event.wait.side_effect = [False, True]

    _poll_loop(client, state, icon, stop_event)  # must not raise
