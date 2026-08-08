"""Tests for app/launcher/gui.py — the parent launcher's lifecycle.

LauncherSupervisor is exercised with injected child supervisors, so the
whole sequence (startup order, health-before-window, restart ordering,
quit, server-failure detection) is provable without spawning real
processes. The child supervisors have their own dedicated test files
covering the real spawn/stop behaviour.
"""

from unittest.mock import MagicMock

import pytest


class _FakeServer:
    def __init__(self, healthy=True, running=True):
        self.session_secret = "secret-" + str(id(self))
        self._healthy = healthy
        self._running = running
        self.started = False
        self.stopped = False
        self.stop_calls = 0

    def start(self):
        self.started = True

    def wait_until_healthy(self, timeout_seconds=None):
        return self._healthy

    def is_running(self):
        return self._running

    def stop(self, timeout_seconds=None):
        self.stopped = True
        self.stop_calls += 1
        self._running = False
        return "graceful"


class _FakeWindow:
    def __init__(self, starts=True):
        self._starts = starts
        self.started = False
        self.stopped = False
        self.shown = False

    def start(self, base_env=None):
        self.started = True
        return self._starts

    def show_or_restart(self, base_env=None):
        self.shown = True
        return True

    def stop(self, timeout_seconds=None):
        self.stopped = True
        return "graceful"


@pytest.fixture
def supervisor_factory(monkeypatch):
    """Builds a LauncherSupervisor whose start_server/start_window create
    the fakes above, recording the order they were called in."""
    from app.launcher import gui

    def _make(server_healthy=True, window_starts=True):
        order = []
        created = {}
        sup = gui.LauncherSupervisor()

        def _start_server():
            order.append("server")
            created["server"] = _FakeServer(healthy=server_healthy)
            sup._server = created["server"]
            created["server"].start()
            return server_healthy

        def _start_window():
            order.append("window")
            created["window"] = _FakeWindow(starts=window_starts)
            sup._window = created["window"]
            created["window"].start()
            return window_starts

        monkeypatch.setattr(sup, "start_server", _start_server)
        monkeypatch.setattr(sup, "start_window", _start_window)
        return sup, order, created

    return _make


# ---------------------------------------------------------------------------
# Startup ordering — the window must never precede a healthy server
# ---------------------------------------------------------------------------

def test_launch_starts_the_server_before_the_window(monkeypatch, supervisor_factory):
    from app.launcher import gui

    sup, order, _ = supervisor_factory()
    monkeypatch.setattr(gui.instance_lock, "check_existing_instance",
                        lambda host, port: gui.instance_lock.InstanceCheckResult(False, False))
    monkeypatch.setattr(gui.instance_lock, "acquire_lock", MagicMock())
    monkeypatch.setattr(gui, "LauncherSupervisor", lambda: sup)

    gui.launch()

    assert order == ["server", "window"], "the window must only start after a healthy server"


def test_launch_never_starts_the_window_when_the_server_is_unhealthy(monkeypatch, supervisor_factory):
    """The core safety property: a window must never come up pointing at
    a server that never became healthy, or it would show a misleading
    connected state."""
    from app.launcher import gui

    sup, order, _ = supervisor_factory(server_healthy=False)
    monkeypatch.setattr(gui.instance_lock, "check_existing_instance",
                        lambda host, port: gui.instance_lock.InstanceCheckResult(False, False))
    monkeypatch.setattr(gui.instance_lock, "acquire_lock", MagicMock())
    monkeypatch.setattr(gui, "LauncherSupervisor", lambda: sup)
    dialog = MagicMock()
    monkeypatch.setattr(gui, "_show_error_dialog", dialog)

    with pytest.raises(SystemExit) as exc:
        gui.launch()

    assert exc.value.code == 1
    assert "window" not in order
    dialog.assert_called_once()


def test_failed_startup_releases_the_lock(monkeypatch, supervisor_factory):
    """No stale lock may survive a failed startup, or the next launch
    would wrongly believe JARVIS is already running."""
    from app.launcher import gui

    sup, _, _ = supervisor_factory(server_healthy=False)
    monkeypatch.setattr(gui.instance_lock, "check_existing_instance",
                        lambda host, port: gui.instance_lock.InstanceCheckResult(False, False))
    monkeypatch.setattr(gui.instance_lock, "acquire_lock", MagicMock())
    release = MagicMock()
    monkeypatch.setattr(gui.instance_lock, "release_lock", release)
    monkeypatch.setattr(gui, "_show_error_dialog", MagicMock())
    monkeypatch.setattr(gui, "LauncherSupervisor", lambda: sup)

    with pytest.raises(SystemExit):
        gui.launch()

    release.assert_called_once()


def test_window_failure_is_degraded_not_fatal(monkeypatch, supervisor_factory):
    """A healthy server with no window must fall back to the browser
    rather than refusing to run."""
    from app.launcher import gui

    sup, order, _ = supervisor_factory(window_starts=False)
    monkeypatch.setattr(gui.instance_lock, "check_existing_instance",
                        lambda host, port: gui.instance_lock.InstanceCheckResult(False, False))
    monkeypatch.setattr(gui.instance_lock, "acquire_lock", MagicMock())
    monkeypatch.setattr(gui, "LauncherSupervisor", lambda: sup)
    opened = MagicMock()
    monkeypatch.setattr(gui.webbrowser, "open", opened)

    result = gui.launch()  # must not raise SystemExit

    assert result is sup
    opened.assert_called_once()


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------

def test_second_launch_creates_nothing_and_opens_the_running_instance(monkeypatch, supervisor_factory):
    from app.launcher import gui

    sup, order, _ = supervisor_factory()
    monkeypatch.setattr(gui.instance_lock, "check_existing_instance",
                        lambda host, port: gui.instance_lock.InstanceCheckResult(True, False, pid=999))
    acquire = MagicMock()
    monkeypatch.setattr(gui.instance_lock, "acquire_lock", acquire)
    monkeypatch.setattr(gui, "LauncherSupervisor", lambda: sup)
    opened = MagicMock()
    monkeypatch.setattr(gui.webbrowser, "open", opened)

    with pytest.raises(SystemExit) as exc:
        gui.launch()

    assert exc.value.code == 0
    assert order == [], "a second launch must not create a server or window"
    acquire.assert_not_called(), "a second launch must not take the lock"
    opened.assert_called_once()


def test_port_held_by_an_unrelated_process_fails_with_a_dialog(monkeypatch, supervisor_factory):
    from app.launcher import gui

    sup, order, _ = supervisor_factory()
    monkeypatch.setattr(gui.instance_lock, "check_existing_instance",
                        lambda host, port: gui.instance_lock.InstanceCheckResult(False, True))
    monkeypatch.setattr(gui, "LauncherSupervisor", lambda: sup)
    dialog = MagicMock()
    monkeypatch.setattr(gui, "_show_error_dialog", dialog)

    with pytest.raises(SystemExit) as exc:
        gui.launch()

    assert exc.value.code == 1
    assert order == []
    assert "port" in dialog.call_args.args[1].lower()


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

def test_restart_stops_window_then_server_then_starts_fresh_ones(supervisor_factory):
    """Ordering matters: no child may be left pointing at a runtime that
    is going away."""
    from app.launcher import gui

    sup = gui.LauncherSupervisor()
    old_server, old_window = _FakeServer(), _FakeWindow()
    sup._server, sup._window = old_server, old_window

    events = []
    sup.start_server = lambda: (events.append("start_server"), True)[1]
    sup.start_window = lambda: (events.append("start_window"), True)[1]
    original_server_stop = old_server.stop
    original_window_stop = old_window.stop
    old_server.stop = lambda timeout_seconds=None: (events.append("stop_server"), original_server_stop(timeout_seconds))[1]
    old_window.stop = lambda timeout_seconds=None: (events.append("stop_window"), original_window_stop(timeout_seconds))[1]

    assert sup.restart() is True
    assert events == ["stop_window", "stop_server", "start_server", "start_window"]


def test_restart_creates_a_fresh_server_with_a_new_session_secret(monkeypatch):
    """Each ServerProcess generates its own secret, so a restart cannot
    reuse the previous session's."""
    from app.launcher import gui, server_process

    sup = gui.LauncherSupervisor()
    monkeypatch.setattr(server_process.ServerProcess, "start", lambda self: None)
    monkeypatch.setattr(server_process.ServerProcess, "wait_until_healthy", lambda self, timeout_seconds=None: True)

    assert sup.start_server() is True
    first_secret = sup.server.session_secret
    assert sup.start_server() is True
    assert sup.server.session_secret != first_secret


def test_restart_reports_failure_when_the_new_server_is_unhealthy():
    from app.launcher import gui

    sup = gui.LauncherSupervisor()
    sup._server, sup._window = _FakeServer(), _FakeWindow()
    sup.start_server = lambda: False
    sup.start_window = lambda: pytest.fail("must not start a window after a failed server start")

    assert sup.restart() is False


# ---------------------------------------------------------------------------
# Quit
# ---------------------------------------------------------------------------

def test_quit_stops_both_children_and_releases_the_lock(monkeypatch):
    from app.launcher import gui

    release = MagicMock()
    monkeypatch.setattr(gui.instance_lock, "release_lock", release)

    sup = gui.LauncherSupervisor()
    server, window = _FakeServer(), _FakeWindow()
    sup._server, sup._window = server, window

    sup.quit()

    assert window.stopped and server.stopped
    release.assert_called_once()


def test_quit_is_idempotent(monkeypatch):
    from app.launcher import gui

    release = MagicMock()
    monkeypatch.setattr(gui.instance_lock, "release_lock", release)
    sup = gui.LauncherSupervisor()
    server = _FakeServer()
    sup._server = server

    sup.quit()
    sup.quit()

    assert server.stop_calls == 1
    release.assert_called_once()


def test_quit_blocks_further_recovery(monkeypatch):
    """Once quitting, a dead server must not be reported as an unexpected
    failure — otherwise a recovery path could resurrect a child during
    shutdown."""
    from app.launcher import gui

    monkeypatch.setattr(gui.instance_lock, "release_lock", MagicMock())
    sup = gui.LauncherSupervisor()
    sup._server = _FakeServer(running=False)

    assert sup.server_failed_unexpectedly() is True
    sup.quit()
    assert sup.quitting is True
    assert sup.server_failed_unexpectedly() is False


# ---------------------------------------------------------------------------
# Server failure detection
# ---------------------------------------------------------------------------

def test_a_running_server_is_not_reported_as_failed():
    from app.launcher import gui
    sup = gui.LauncherSupervisor()
    sup._server = _FakeServer(running=True)
    assert sup.server_failed_unexpectedly() is False


def test_an_unexpected_server_exit_is_detected():
    from app.launcher import gui
    sup = gui.LauncherSupervisor()
    sup._server = _FakeServer(running=False)
    assert sup.server_failed_unexpectedly() is True


# ---------------------------------------------------------------------------
# Open / focus
# ---------------------------------------------------------------------------

def test_open_or_focus_shows_an_existing_window():
    from app.launcher import gui
    sup = gui.LauncherSupervisor()
    window = _FakeWindow()
    sup._window = window

    assert sup.open_or_focus_window() is True
    assert window.shown is True


def test_open_or_focus_starts_a_window_when_there_is_none(monkeypatch):
    from app.launcher import gui
    sup = gui.LauncherSupervisor()
    started = {}
    monkeypatch.setattr(sup, "start_window", lambda: started.setdefault("yes", True))

    assert sup.open_or_focus_window() is True
    assert started == {"yes": True}


# ---------------------------------------------------------------------------
# Error dialog content — safe by construction
# ---------------------------------------------------------------------------

def test_error_message_carries_a_correlation_id_and_a_log_folder():
    from app.launcher import gui
    message = gui._format_error_message("Something broke.", "abc-123")
    assert "Something broke." in message
    assert "abc-123" in message
    assert "Logs:" in message


def test_error_message_never_contains_a_secret(monkeypatch):
    """A crash dialog is user-visible; it must never leak the session
    secret or an API key."""
    from app.launcher import gui
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-appear")
    message = gui._format_error_message("Startup failed.", "corr-1")
    assert "sk-" not in message


def test_show_error_dialog_off_windows_does_not_raise(monkeypatch):
    from app.launcher import gui
    monkeypatch.setattr(gui.sys, "platform", "linux")
    gui._show_error_dialog("Title", "Message")  # must not raise


# ---------------------------------------------------------------------------
# dashboard_url()
# ---------------------------------------------------------------------------

def test_dashboard_url_points_at_setup_before_onboarding_completes(monkeypatch):
    from app.launcher import gui
    monkeypatch.setattr("app.core.onboarding.is_onboarding_complete", lambda: False)
    assert gui.dashboard_url().endswith("/ui/setup")


def test_dashboard_url_points_at_the_dashboard_after_onboarding(monkeypatch):
    from app.launcher import gui
    monkeypatch.setattr("app.core.onboarding.is_onboarding_complete", lambda: True)
    assert gui.dashboard_url().endswith("/ui/")
