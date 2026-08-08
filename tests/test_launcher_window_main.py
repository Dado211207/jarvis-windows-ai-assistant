"""Tests for app/launcher/window_main.py — the `--window` child entry.

pywebview is injected as a fake module (the same seam
tests/test_launcher_webview_window.py already uses), so every branch —
missing parent context, failed authentication, each IPC command, and the
user-close vs parent-quit distinction — is provable on Linux CI without a
real WebView2 desktop.
"""

from unittest.mock import MagicMock

import pytest

from app.launcher import ipc


class _FakeWindow:
    def __init__(self):
        self.show = MagicMock()
        self.hide = MagicMock()
        self.restore = MagicMock()
        self.destroy = MagicMock()
        self.load_url = MagicMock()
        self.get_current_url = MagicMock(return_value="http://127.0.0.1:5555/ui/")


@pytest.fixture(autouse=True)
def _reset_window_state():
    from app.launcher import webview_window
    webview_window._window = None
    webview_window._shutdown_requested.clear()
    yield
    webview_window._window = None
    webview_window._shutdown_requested.clear()


# ---------------------------------------------------------------------------
# Failing safe when launched without a parent
# ---------------------------------------------------------------------------

def test_missing_parent_context_exits_cleanly_without_a_traceback(capsys, monkeypatch):
    """`JARVIS.exe --window` double-clicked by a user has no inherited
    IPC context. It must say so and exit, not crash and not open a
    window pointing at a server that isn't there."""
    from app.launcher import window_main

    # context=None means "read the environment"; force that lookup to find
    # nothing rather than depending on this test process's own env.
    monkeypatch.setattr(window_main.ipc, "child_context_from_env", lambda env=None: None)

    code = window_main.main(argv=["--window"], webview_module=MagicMock())

    assert code == 2
    assert "not meant to be started directly" in capsys.readouterr().err


def test_failed_authentication_returns_a_distinct_exit_code(monkeypatch):
    from app.launcher import window_main

    def _boom(*args, **kwargs):
        raise PermissionError("digest mismatch")

    monkeypatch.setattr(window_main.ipc, "ControlClient", _boom)

    code = window_main.main(
        context={"address": ("127.0.0.1", 1), "secret": b"x", "url": "http://127.0.0.1:5555/ui/", "close_action": "tray"},
        webview_module=MagicMock(),
    )

    assert code == 3, "authentication failure must be distinguishable from a normal exit"


# ---------------------------------------------------------------------------
# Command handling
# ---------------------------------------------------------------------------

def test_show_command_shows_and_restores_the_window():
    from app.launcher.window_main import _handle_command

    window = _FakeWindow()
    assert _handle_command(ipc.COMMAND_SHOW, window, "tray") is True
    window.show.assert_called_once()
    window.restore.assert_called_once()


def test_hide_command_hides_the_window():
    from app.launcher.window_main import _handle_command

    window = _FakeWindow()
    assert _handle_command(ipc.COMMAND_HIDE, window, "tray") is True
    window.hide.assert_called_once()
    window.show.assert_not_called()


def test_focus_command_restores_without_re_showing():
    from app.launcher.window_main import _handle_command

    window = _FakeWindow()
    assert _handle_command(ipc.COMMAND_FOCUS, window, "tray") is True
    window.restore.assert_called_once()


def test_reload_command_reloads_the_current_url():
    from app.launcher.window_main import _handle_command

    window = _FakeWindow()
    assert _handle_command(ipc.COMMAND_RELOAD, window, "tray") is True
    window.load_url.assert_called_once_with("http://127.0.0.1:5555/ui/")


def test_reload_failure_does_not_stop_the_command_loop():
    from app.launcher.window_main import _handle_command

    window = _FakeWindow()
    window.load_url.side_effect = RuntimeError("renderer is gone")
    assert _handle_command(ipc.COMMAND_RELOAD, window, "tray") is True


# ---------------------------------------------------------------------------
# The user-close vs parent-quit distinction
# ---------------------------------------------------------------------------

def test_quit_command_stops_the_loop_and_destroys_the_window():
    from app.launcher.window_main import _handle_command

    window = _FakeWindow()
    assert _handle_command(ipc.COMMAND_QUIT, window, "tray") is False
    window.destroy.assert_called_once()


def test_quit_command_overrides_close_to_tray():
    """The core distinction this architecture needs: with
    close_action="tray" a user's X click hides the window, but a parent
    Quit must still close it. request_shutdown() is what tells the close
    handler not to veto."""
    from app.launcher import webview_window
    from app.launcher.window_main import _handle_command

    assert webview_window._shutdown_requested.is_set() is False
    _handle_command(ipc.COMMAND_QUIT, _FakeWindow(), "tray")
    assert webview_window._shutdown_requested.is_set() is True


def test_quit_survives_a_window_that_refuses_to_be_destroyed():
    from app.launcher.window_main import _handle_command

    window = _FakeWindow()
    window.destroy.side_effect = RuntimeError("already gone")
    assert _handle_command(ipc.COMMAND_QUIT, window, "tray") is False  # must not raise


# ---------------------------------------------------------------------------
# Command pump
# ---------------------------------------------------------------------------

def test_command_pump_stops_on_quit():
    import threading

    from app.launcher.window_main import _command_pump

    class _Client:
        def __init__(self):
            self.sent = ["show", "quit"]

        def poll_command(self, timeout=0.0):
            return self.sent.pop(0) if self.sent else None

    stop_event = threading.Event()
    window = _FakeWindow()

    _command_pump(_Client(), window, "tray", stop_event)

    assert stop_event.is_set()
    window.show.assert_called_once()
    window.destroy.assert_called_once()


def test_command_pump_ignores_none_and_keeps_going():
    import threading

    from app.launcher.window_main import _command_pump

    class _Client:
        def __init__(self):
            self.calls = 0

        def poll_command(self, timeout=0.0):
            self.calls += 1
            if self.calls < 3:
                return None
            return ipc.COMMAND_QUIT

    stop_event = threading.Event()
    _command_pump(_Client(), _FakeWindow(), "tray", stop_event)

    assert stop_event.is_set()
