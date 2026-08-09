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


# ---------------------------------------------------------------------------
# Runtime preflight, and what READY is allowed to mean
# ---------------------------------------------------------------------------

class _RecordingClient:
    """Captures everything the child reports to its parent."""

    def __init__(self, commands=None):
        self.events = []
        self.closed = False
        self._commands = list(commands or [])

    def send_event(self, event, **fields):
        self.events.append({"event": event, **fields})
        return True

    def poll_command(self, timeout=0.0):
        return self._commands.pop(0) if self._commands else None

    def close(self):
        self.closed = True

    def details_for(self, event):
        return [e.get("detail") for e in self.events if e["event"] == event]


_CONTEXT = {
    "address": ("127.0.0.1", 1),
    "secret": b"x",
    "url": "http://127.0.0.1:5555/ui/",
    "close_action": "tray",
}


def test_a_missing_runtime_is_reported_before_any_gui_work(monkeypatch):
    """Checked up front so a missing WebView2 produces a named cause the
    parent can offer a fix for, rather than an exception from deep inside
    pywebview that nobody can act on."""
    from app.launcher import runtime_check, window_main

    client = _RecordingClient()
    monkeypatch.setattr(window_main.ipc, "ControlClient", lambda *a, **k: client)
    monkeypatch.setattr(runtime_check, "window_runtime_error", lambda: ipc.ERROR_WEBVIEW2_MISSING)
    webview_module = MagicMock()

    code = window_main.main(context=dict(_CONTEXT), webview_module=webview_module)

    assert code == 4
    assert client.details_for(ipc.EVENT_ERROR) == [ipc.ERROR_WEBVIEW2_MISSING]
    assert ipc.EVENT_READY not in [e["event"] for e in client.events]
    assert client.closed is True


def test_the_preflight_runs_before_the_window_is_created(monkeypatch):
    from app.launcher import runtime_check, webview_window, window_main

    client = _RecordingClient()
    monkeypatch.setattr(window_main.ipc, "ControlClient", lambda *a, **k: client)
    monkeypatch.setattr(runtime_check, "window_runtime_error", lambda: ipc.ERROR_DOTNET_MISSING)
    create = MagicMock()
    monkeypatch.setattr(webview_window, "create_and_run", create)

    window_main.main(context=dict(_CONTEXT), webview_module=MagicMock())

    create.assert_not_called()


def test_ready_is_only_sent_once_a_window_object_really_exists(monkeypatch):
    """The regression that made the browser look like the product: READY
    used to be sent before create_and_run(), so a machine that could
    never build a window still reported a healthy start — and the parent,
    seeing no window, opened a browser instead."""
    from app.launcher import runtime_check, webview_window, window_main

    client = _RecordingClient()
    monkeypatch.setattr(window_main.ipc, "ControlClient", lambda *a, **k: client)
    monkeypatch.setattr(runtime_check, "window_runtime_error", lambda: None)

    events_at_creation = {}

    def _create_and_run(**kwargs):
        events_at_creation["before"] = [e["event"] for e in client.events]
        webview_window._window = _FakeWindow()
        # Let the pump thread notice the window, then end the "GUI loop".
        import time
        for _ in range(100):
            if any(e["event"] == ipc.EVENT_READY for e in client.events):
                return
            time.sleep(0.02)

    monkeypatch.setattr(webview_window, "create_and_run", _create_and_run)

    code = window_main.main(context=dict(_CONTEXT), webview_module=MagicMock())

    assert code == 0
    assert events_at_creation["before"] == [], "nothing may be claimed before the window is attempted"
    assert ipc.EVENT_READY in [e["event"] for e in client.events]


def test_a_window_that_never_appears_reports_an_error_rather_than_ready(monkeypatch):
    from app.launcher import runtime_check, webview_window, window_main

    client = _RecordingClient()
    monkeypatch.setattr(window_main.ipc, "ControlClient", lambda *a, **k: client)
    monkeypatch.setattr(runtime_check, "window_runtime_error", lambda: None)
    monkeypatch.setattr(webview_window, "current_window", lambda: None)
    # Two very short polls: the real values would make this test a
    # ten-second wait for a decision that is already determined.
    monkeypatch.setattr(window_main, "WINDOW_READY_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(window_main, "WINDOW_READY_POLL_INTERVAL_SECONDS", 0.01)

    def _create_and_run(**kwargs):
        import time
        for _ in range(100):
            if client.events:
                return
            time.sleep(0.02)

    monkeypatch.setattr(webview_window, "create_and_run", _create_and_run)

    window_main.main(context=dict(_CONTEXT), webview_module=MagicMock())

    assert client.details_for(ipc.EVENT_ERROR) == [ipc.ERROR_WINDOW_FAILED]
    assert ipc.EVENT_READY not in [e["event"] for e in client.events]


def test_a_crash_inside_pywebview_reports_a_named_cause(monkeypatch):
    """A free-text reason could never be matched to a repair. Every
    failure the parent sees is one of ipc.VALID_ERROR_DETAILS."""
    from app.launcher import runtime_check, webview_window, window_main

    client = _RecordingClient()
    monkeypatch.setattr(window_main.ipc, "ControlClient", lambda *a, **k: client)
    monkeypatch.setattr(runtime_check, "window_runtime_error", lambda: None)
    monkeypatch.setattr(webview_window, "create_and_run",
                        MagicMock(side_effect=RuntimeError("edgechromium is unavailable")))

    code = window_main.main(context=dict(_CONTEXT), webview_module=MagicMock())

    assert code == 4
    reported = client.details_for(ipc.EVENT_ERROR)
    assert reported and set(reported) <= ipc.VALID_ERROR_DETAILS
