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
        on_open_in_browser=lambda: calls.append("browser"),
        on_toggle_privacy=lambda: calls.append("privacy"),
        on_restart=lambda: calls.append("restart"),
        on_quit=lambda: calls.append("quit"),
    )

    labels = [e.label for e in entries]
    assert labels == [
        "Status: running",
        "Open JARVIS",
        "Open in Browser",
        "Open Command Center",
        "Privacy mode: OFF",
        "Restart JARVIS",
        "Quit JARVIS",
    ]
    # Status entry is a display-only label — no action, disabled.
    assert entries[0].action is None
    assert entries[0].enabled is False

    # Every action-bearing entry actually invokes the callback it was given.
    expected_order = ["dashboard", "browser", "command_center", "privacy", "restart", "quit"]
    for entry in entries[1:]:
        entry.action()
    assert calls == expected_order


def test_privacy_entry_disabled_when_state_unknown():
    from app.launcher.tray import TrayState, build_menu_entries

    entries = build_menu_entries(
        TrayState(privacy_active=None),
        on_open_dashboard=lambda: None,
        on_open_command_center=lambda: None,
        on_open_in_browser=lambda: None,
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


# ---------------------------------------------------------------------------
# _attention_loop — a second launch must surface the native window
# ---------------------------------------------------------------------------

def _one_tick_stop_event():
    """A stop_event whose .wait() returns True (meaning "stop requested")
    on the second call, so a loop body runs exactly once."""
    stop_event = MagicMock(spec=threading.Event)
    stop_event.wait.side_effect = [False, True]
    return stop_event


def test_attention_loop_shows_the_window_when_a_launch_asks(monkeypatch):
    from app.launcher import attention, tray

    monkeypatch.setattr(attention, "consume", MagicMock(side_effect=[True]))
    on_attention = MagicMock()

    tray._attention_loop(on_attention, _one_tick_stop_event())

    on_attention.assert_called_once()


def test_attention_loop_does_nothing_without_a_request(monkeypatch):
    from app.launcher import attention, tray

    monkeypatch.setattr(attention, "consume", MagicMock(return_value=False))
    on_attention = MagicMock()

    tray._attention_loop(on_attention, _one_tick_stop_event())

    on_attention.assert_not_called()


def test_attention_loop_survives_a_handler_that_raises(monkeypatch):
    """A marker that cannot be turned into a window must not take the
    thread down with it — the next click would then be ignored forever."""
    from app.launcher import attention, tray

    monkeypatch.setattr(attention, "consume", MagicMock(return_value=True))

    tray._attention_loop(MagicMock(side_effect=RuntimeError("no window")), _one_tick_stop_event())


def test_attention_is_checked_far_more_often_than_health(monkeypatch):
    """Someone who just double-clicked the shortcut is watching for a
    window right now; a five-second wait reads as "nothing happened"."""
    from app.launcher import tray

    assert tray.ATTENTION_POLL_INTERVAL_SECONDS <= 1.0
    assert tray.ATTENTION_POLL_INTERVAL_SECONDS < tray.POLL_INTERVAL_SECONDS


def test_attention_loop_waits_on_its_own_interval(monkeypatch):
    from app.launcher import attention, tray

    monkeypatch.setattr(attention, "consume", MagicMock(return_value=False))
    stop_event = _one_tick_stop_event()

    tray._attention_loop(MagicMock(), stop_event)

    assert stop_event.wait.call_args_list[0].args == (tray.ATTENTION_POLL_INTERVAL_SECONDS,)


# ---------------------------------------------------------------------------
# Restart failure reporting — the message must match what actually failed
# ---------------------------------------------------------------------------

def test_each_restart_stage_gets_its_own_message(monkeypatch):
    """The reported defect: every restart failure produced the same
    sentence regardless of cause, which made the one real report of it
    impossible to act on."""
    from app.launcher import gui, tray

    seen = {}
    monkeypatch.setattr(gui, "_show_error_dialog", lambda title, message: seen.setdefault(title, []).append(message))

    for stage in ("port_busy", "server_unhealthy", ""):
        tray._show_restart_failure(stage)

    messages = seen["JARVIS couldn't restart"]
    assert len(set(messages)) == 3, "distinct causes must not collapse into one message"
    assert "port" in messages[0].lower()


def test_a_port_conflict_is_not_reported_as_a_dead_runtime(monkeypatch):
    from app.launcher import gui, tray

    captured = {}
    monkeypatch.setattr(gui, "_show_error_dialog", lambda title, message: captured.update(message=message))

    tray._show_restart_failure("port_busy")

    assert "did not come back up" not in captured["message"]


def test_a_window_only_failure_says_the_runtime_is_fine(monkeypatch):
    """The exact wording from the hardware test — "the JARVIS runtime did
    not come back up ... use Quit and start JARVIS again" — sent the user
    to fix a runtime that was healthy."""
    from app.launcher import gui, ipc, runtime_check, tray

    captured = {}
    monkeypatch.setattr(gui, "_show_error_dialog", lambda title, message: captured.update(title=title, message=message))

    tray._show_window_failure(ipc.ERROR_WEBVIEW2_MISSING)

    assert captured["title"] == "JARVIS couldn't open its window"
    assert "WebView2" in captured["message"]
    assert runtime_check.WEBVIEW2_DOWNLOAD_URL in captured["message"]
    assert "running normally" in captured["message"]
    assert "did not come back up" not in captured["message"]


def test_a_generic_window_failure_offers_no_misleading_download(monkeypatch):
    from app.launcher import gui, ipc, runtime_check, tray

    captured = {}
    monkeypatch.setattr(gui, "_show_error_dialog", lambda title, message: captured.update(message=message))

    tray._show_window_failure(ipc.ERROR_WINDOW_FAILED)

    assert runtime_check.WEBVIEW2_DOWNLOAD_URL not in captured["message"]


def test_failure_messages_carry_a_reference_id_and_a_log_folder(monkeypatch):
    """Both dialogs go through gui._format_error_message, so a user can
    quote a reference ID and find the logs without being shown a
    traceback."""
    from app.launcher import gui, ipc, tray

    captured = []
    monkeypatch.setattr(gui, "_show_error_dialog", lambda title, message: captured.append(message))

    tray._show_restart_failure("server_unhealthy")
    tray._show_window_failure(ipc.ERROR_WINDOW_FAILED)

    for message in captured:
        assert "Reference ID:" in message
        assert "Logs:" in message
        assert "sk-" not in message


# ---------------------------------------------------------------------------
# Quit's force-exit backstop
# ---------------------------------------------------------------------------

def test_the_force_exit_backstop_cannot_cut_a_working_shutdown_short():
    """Quit arms a watchdog so JARVIS can never become unclosable. Its
    grace period has to exceed the worst case of every bounded step
    below it, or a slow-but-working shutdown would be killed mid-way and
    leave exactly the orphaned children the watchdog exists to prevent.

    Worst case, from gui.LauncherSupervisor.quit(): each stop() escalates
    quit -> terminate -> kill on its own budget (three times the
    configured timeout), then the port release is waited out."""
    from app.launcher import gui, tray

    worst_case = (
        3 * gui.QUIT_WINDOW_STOP_TIMEOUT_SECONDS
        + 3 * gui.QUIT_SERVER_STOP_TIMEOUT_SECONDS
        + gui.QUIT_PORT_RELEASE_TIMEOUT_SECONDS
    )

    assert tray.QUIT_FORCE_EXIT_SECONDS > worst_case, (
        f"the backstop ({tray.QUIT_FORCE_EXIT_SECONDS}s) must exceed the bounded "
        f"worst case ({worst_case}s)"
    )


def test_the_force_exit_backstop_is_actually_reachable():
    """It was previously a tested function that nothing in production
    called — a guarantee on paper only."""
    import inspect

    from app.launcher import tray

    source = inspect.getsource(tray.run_tray_loop)

    assert "force_exit_after(QUIT_FORCE_EXIT_SECONDS)" in source
    quit_body = source.split("def do_quit()", 1)[1].split("def show_context_menu", 1)[0]
    assert quit_body.index("force_exit_after") < quit_body.index("supervisor.quit()"), (
        "the backstop must be armed before the shutdown it is guarding"
    )
