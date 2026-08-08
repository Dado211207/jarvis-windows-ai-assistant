"""Tests for app/launcher/gui.py's pre-tray startup orchestration.

instance_lock and server_runner are mocked here — they have their own
dedicated test files exercising the real logic. This file only proves
gui.launch() sequences and branches on their results correctly, never
starts two servers, and never lets a raw exception/traceback surface.
"""

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest


@dataclass
class _FakeRunningServer:
    request_shutdown: MagicMock
    shut_down_called: bool = False


def _fake_running_server() -> _FakeRunningServer:
    return _FakeRunningServer(request_shutdown=MagicMock())


# ---------------------------------------------------------------------------
# launch() — already running
# ---------------------------------------------------------------------------

def test_launch_opens_existing_dashboard_and_exits_cleanly_when_already_running(monkeypatch):
    from app.launcher import gui

    monkeypatch.setattr(
        gui.instance_lock, "check_existing_instance",
        lambda host, port: gui.instance_lock.InstanceCheckResult(another_instance_running=True, port_in_use_by_other=False, pid=777),
    )
    started = MagicMock()
    monkeypatch.setattr(gui.server_runner, "start_server_in_background", started)
    opened = MagicMock()
    monkeypatch.setattr(gui.webbrowser, "open", opened)

    with pytest.raises(SystemExit) as exc_info:
        gui.launch()

    assert exc_info.value.code == 0
    started.assert_not_called()
    opened.assert_called_once_with(gui.dashboard_url())


# ---------------------------------------------------------------------------
# launch() — port held by something else
# ---------------------------------------------------------------------------

def test_launch_fails_cleanly_when_port_used_by_unrelated_process(monkeypatch):
    from app.launcher import gui

    monkeypatch.setattr(
        gui.instance_lock, "check_existing_instance",
        lambda host, port: gui.instance_lock.InstanceCheckResult(another_instance_running=False, port_in_use_by_other=True),
    )
    started = MagicMock()
    monkeypatch.setattr(gui.server_runner, "start_server_in_background", started)
    dialog = MagicMock()
    monkeypatch.setattr(gui, "_show_error_dialog", dialog)

    with pytest.raises(SystemExit) as exc_info:
        gui.launch()

    assert exc_info.value.code == 1
    started.assert_not_called()
    dialog.assert_called_once()
    assert "port" in dialog.call_args.args[1].lower() or "port" in dialog.call_args.args[1]


# ---------------------------------------------------------------------------
# launch() — success
# ---------------------------------------------------------------------------

def test_launch_success_acquires_lock_and_starts_server_without_opening_anything(monkeypatch):
    """launch() itself no longer decides how the UI is presented — that's
    run_windowed()'s job now (native window, falling back to the
    browser). launch() succeeding must not have any UI side effect of
    its own; see test_open_main_window_* below for the actual
    window-vs-browser decision."""
    from app.launcher import gui

    monkeypatch.setattr(
        gui.instance_lock, "check_existing_instance",
        lambda host, port: gui.instance_lock.InstanceCheckResult(another_instance_running=False, port_in_use_by_other=False),
    )
    acquire = MagicMock()
    monkeypatch.setattr(gui.instance_lock, "acquire_lock", acquire)

    fake_running = _fake_running_server()
    monkeypatch.setattr(gui.server_runner, "start_server_in_background", lambda: fake_running)
    monkeypatch.setattr(gui.server_runner, "wait_until_healthy", lambda: True)

    opened = MagicMock()
    monkeypatch.setattr(gui.webbrowser, "open", opened)

    result = gui.launch()

    assert result is fake_running
    acquire.assert_called_once()
    opened.assert_not_called()


# ---------------------------------------------------------------------------
# launch() — health-wait timeout cleans up instead of hanging around
# ---------------------------------------------------------------------------

def test_launch_cleans_up_and_fails_when_never_healthy(monkeypatch):
    from app.launcher import gui

    monkeypatch.setattr(
        gui.instance_lock, "check_existing_instance",
        lambda host, port: gui.instance_lock.InstanceCheckResult(another_instance_running=False, port_in_use_by_other=False),
    )
    monkeypatch.setattr(gui.instance_lock, "acquire_lock", MagicMock())
    release = MagicMock()
    monkeypatch.setattr(gui.instance_lock, "release_lock", release)

    fake_running = _fake_running_server()
    monkeypatch.setattr(gui.server_runner, "start_server_in_background", lambda: fake_running)
    monkeypatch.setattr(gui.server_runner, "wait_until_healthy", lambda: False)

    dialog = MagicMock()
    monkeypatch.setattr(gui, "_show_error_dialog", dialog)
    opened = MagicMock()
    monkeypatch.setattr(gui.webbrowser, "open", opened)

    with pytest.raises(SystemExit) as exc_info:
        gui.launch()

    assert exc_info.value.code == 1
    fake_running.request_shutdown.assert_called_once()
    release.assert_called_once()
    dialog.assert_called_once()
    assert "healthy" in dialog.call_args.args[1].lower()
    # The dashboard must never open on an unhealthy server — opening is
    # gated strictly behind a successful health-wait, not just attempted
    # unconditionally after starting the server.
    opened.assert_not_called()


# ---------------------------------------------------------------------------
# shutdown()
# ---------------------------------------------------------------------------

def test_shutdown_stops_server_and_releases_lock(monkeypatch):
    from app.launcher import gui

    release = MagicMock()
    monkeypatch.setattr(gui.instance_lock, "release_lock", release)
    fake_running = _fake_running_server()

    gui.shutdown(fake_running)

    fake_running.request_shutdown.assert_called_once()
    release.assert_called_once()


# ---------------------------------------------------------------------------
# Error message formatting and dialog dispatch
# ---------------------------------------------------------------------------

def test_format_error_message_includes_reason_and_correlation_id():
    from app.launcher import gui
    msg = gui._format_error_message("Something broke.", "abc-123")
    assert "Something broke." in msg
    assert "abc-123" in msg
    assert "Log file" in msg


def test_show_error_dialog_on_non_windows_does_not_raise(monkeypatch):
    from app.launcher import gui
    monkeypatch.setattr(gui.sys, "platform", "linux")
    gui._show_error_dialog("Title", "Message")  # must not raise


def test_show_error_dialog_on_windows_calls_message_box(monkeypatch):
    import ctypes
    from app.launcher import gui

    monkeypatch.setattr(gui.sys, "platform", "win32")
    fake_message_box = MagicMock()
    fake_user32 = MagicMock(MessageBoxW=fake_message_box)
    fake_windll = MagicMock(user32=fake_user32)
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)

    gui._show_error_dialog("Title", "Message")

    fake_message_box.assert_called_once()
    args = fake_message_box.call_args.args
    assert args[1] == "Message"
    assert args[2] == "Title"


# ---------------------------------------------------------------------------
# dashboard_url() — routes through first-run setup until it's complete
# ---------------------------------------------------------------------------

def test_dashboard_url_points_at_setup_before_onboarding_complete(monkeypatch):
    from app.launcher import gui
    monkeypatch.setattr("app.core.onboarding.is_onboarding_complete", lambda: False)
    assert gui.dashboard_url().endswith("/ui/setup")


def test_dashboard_url_points_at_dashboard_after_onboarding_complete(monkeypatch):
    from app.launcher import gui
    monkeypatch.setattr("app.core.onboarding.is_onboarding_complete", lambda: True)
    assert gui.dashboard_url().endswith("/ui/")
    assert not gui.dashboard_url().endswith("/ui/setup")


# ---------------------------------------------------------------------------
# _open_main_window() — native window, falling back to the browser.
# _open_main_window() itself does `from app.launcher import webview_window`
# fresh on every call, so patching the real module's attributes (not an
# attribute on the `gui` module) is what actually takes effect here.
# ---------------------------------------------------------------------------

def test_open_main_window_uses_the_native_window_when_supported(monkeypatch):
    from app.launcher import gui, webview_window

    monkeypatch.setattr(webview_window, "is_supported", lambda: True)
    create_and_run = MagicMock()
    monkeypatch.setattr(webview_window, "create_and_run", create_and_run)
    opened = MagicMock()
    monkeypatch.setattr(gui.webbrowser, "open", opened)
    monkeypatch.setattr("app.core.onboarding.is_onboarding_complete", lambda: True)
    monkeypatch.setattr(gui.settings, "jarvis_close_action", "tray")

    fake_running = _fake_running_server()
    request_tray_quit = MagicMock()
    gui._open_main_window(fake_running, request_tray_quit)

    create_and_run.assert_called_once()
    assert create_and_run.call_args.kwargs["url"] == gui.dashboard_url()
    assert create_and_run.call_args.kwargs["close_action"] == "tray"
    opened.assert_not_called()


def test_open_main_window_falls_back_to_browser_when_not_supported(monkeypatch):
    from app.launcher import gui, webview_window

    monkeypatch.setattr(webview_window, "is_supported", lambda: False)
    create_and_run = MagicMock()
    monkeypatch.setattr(webview_window, "create_and_run", create_and_run)
    opened = MagicMock()
    monkeypatch.setattr(gui.webbrowser, "open", opened)

    gui._open_main_window(_fake_running_server(), MagicMock())

    create_and_run.assert_not_called()
    opened.assert_called_once_with(gui.dashboard_url())


def test_open_main_window_falls_back_to_browser_when_native_window_raises(monkeypatch):
    """The core safety property from this milestone's own spec: JARVIS
    must keep working even when the native shell can't be created for
    some reason (WebView2 Runtime absent, pythonnet/.NET missing, ...) —
    never a hard failure, never a silently broken/insecure window."""
    from app.launcher import gui, webview_window

    monkeypatch.setattr(webview_window, "is_supported", lambda: True)
    monkeypatch.setattr(webview_window, "create_and_run", MagicMock(side_effect=RuntimeError("no WebView2 runtime")))
    opened = MagicMock()
    monkeypatch.setattr(gui.webbrowser, "open", opened)

    gui._open_main_window(_fake_running_server(), MagicMock())

    opened.assert_called_once_with(gui.dashboard_url())


def test_open_main_window_on_quit_callback_shuts_down_and_requests_tray_quit(monkeypatch):
    """The window's on_quit callback (only invoked by webview_window when
    close_action="quit") must tear down both halves of the app: the
    server/lock (shutdown()) and the tray running on its own thread
    (request_tray_quit()) — otherwise one half would be left running
    with the other gone."""
    from app.launcher import gui, webview_window

    monkeypatch.setattr(webview_window, "is_supported", lambda: True)
    captured = {}

    def _fake_create_and_run(**kwargs):
        captured["on_quit"] = kwargs["on_quit"]

    monkeypatch.setattr(webview_window, "create_and_run", _fake_create_and_run)
    shutdown_mock = MagicMock()
    monkeypatch.setattr(gui, "shutdown", shutdown_mock)

    fake_running = _fake_running_server()
    request_tray_quit = MagicMock()
    gui._open_main_window(fake_running, request_tray_quit)

    assert "on_quit" in captured
    captured["on_quit"]()

    shutdown_mock.assert_called_once_with(fake_running)
    request_tray_quit.assert_called_once()
