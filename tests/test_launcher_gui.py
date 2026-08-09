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
    def __init__(self, healthy=True, running=True, port_released=True):
        self.session_secret = "secret-" + str(id(self))
        self._healthy = healthy
        self._running = running
        self._port_released = port_released
        self.started = False
        self.stopped = False
        self.stop_calls = 0

    def start(self):
        self.started = True

    def wait_until_healthy(self, timeout_seconds=None):
        return self._healthy

    def is_running(self):
        return self._running

    def wait_until_port_released(self, timeout_seconds=None):
        return self._port_released

    def stop(self, timeout_seconds=None):
        self.stopped = True
        self.stop_calls += 1
        self._running = False
        return "graceful"


class _FakeWindow:
    def __init__(self, starts=True, running=True, shows=True):
        self._starts = starts
        self._running = running
        self._shows = shows
        self.started = False
        self.stopped = False
        self.shown = False

    def start(self, base_env=None):
        self.started = True
        return self._starts

    def start_detailed(self, base_env=None):
        from app.launcher.window_process import WindowStartResult
        self.started = True
        return WindowStartResult(self._starts, "" if self._starts else "window_failed")

    def is_running(self):
        return self._running

    def show(self):
        self.shown = self._shows
        return self._shows

    def show_or_restart(self, base_env=None):
        self.shown = True
        return True

    def stop(self, timeout_seconds=None):
        self.stopped = True
        self._running = False
        return "graceful"


@pytest.fixture
def supervisor_factory(monkeypatch):
    """Builds a LauncherSupervisor whose start_server/start_window create
    the fakes above, recording the order they were called in."""
    from app.launcher import gui

    def _make(server_healthy=True, window_starts=True, window_reason="window_failed"):
        from app.launcher.window_process import WindowStartResult

        order = []
        created = {}
        sup = gui.LauncherSupervisor()

        def _start_server():
            order.append("server")
            created["server"] = _FakeServer(healthy=server_healthy)
            sup._server = created["server"]
            created["server"].start()
            return server_healthy

        def _start_window_detailed():
            order.append("window")
            created["window"] = _FakeWindow(starts=window_starts)
            sup._window = created["window"] if window_starts else None
            created["window"].start()
            return WindowStartResult(window_starts, "" if window_starts else window_reason)

        monkeypatch.setattr(sup, "start_server", _start_server)
        monkeypatch.setattr(sup, "start_window_detailed", _start_window_detailed)
        monkeypatch.setattr(sup, "start_window", lambda: _start_window_detailed().ok)
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
    """A healthy server with no window must keep running (the tray is
    still a usable control surface) rather than refusing to start."""
    from app.launcher import gui

    sup, order, _ = supervisor_factory(window_starts=False)
    monkeypatch.setattr(gui.instance_lock, "check_existing_instance",
                        lambda host, port: gui.instance_lock.InstanceCheckResult(False, False))
    monkeypatch.setattr(gui.instance_lock, "acquire_lock", MagicMock())
    monkeypatch.setattr(gui, "LauncherSupervisor", lambda: sup)
    monkeypatch.setattr(gui, "_show_error_dialog", MagicMock())

    result = gui.launch()  # must not raise SystemExit

    assert result is sup


def test_window_failure_never_silently_opens_a_browser(monkeypatch, supervisor_factory):
    """The reported defect: a window that could not be created was
    answered by quietly opening a browser tab, which is how the browser
    came to look like the product's real interface. The user must be told
    what is missing instead."""
    import webbrowser

    from app.launcher import gui

    sup, _, _ = supervisor_factory(window_starts=False)
    monkeypatch.setattr(gui.instance_lock, "check_existing_instance",
                        lambda host, port: gui.instance_lock.InstanceCheckResult(False, False))
    monkeypatch.setattr(gui.instance_lock, "acquire_lock", MagicMock())
    monkeypatch.setattr(gui, "LauncherSupervisor", lambda: sup)
    opened = MagicMock()
    monkeypatch.setattr(webbrowser, "open", opened)
    dialog = MagicMock()
    monkeypatch.setattr(gui, "_show_error_dialog", dialog)

    gui.launch()

    opened.assert_not_called()
    dialog.assert_called_once()


def test_a_missing_webview2_runtime_is_named_with_its_download_link(monkeypatch, supervisor_factory):
    """"Something went wrong" and "install the WebView2 runtime, here is
    the link" are different problems with different fixes."""
    from app.launcher import gui, ipc, runtime_check

    sup, _, _ = supervisor_factory(window_starts=False, window_reason=ipc.ERROR_WEBVIEW2_MISSING)
    monkeypatch.setattr(gui.instance_lock, "check_existing_instance",
                        lambda host, port: gui.instance_lock.InstanceCheckResult(False, False))
    monkeypatch.setattr(gui.instance_lock, "acquire_lock", MagicMock())
    monkeypatch.setattr(gui, "LauncherSupervisor", lambda: sup)
    dialog = MagicMock()
    monkeypatch.setattr(gui, "_show_error_dialog", dialog)

    gui.launch()

    message = dialog.call_args.args[1]
    assert "WebView2" in message
    assert runtime_check.WEBVIEW2_DOWNLOAD_URL in message


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------

def test_second_launch_creates_nothing_and_asks_the_running_one_to_show_itself(monkeypatch, supervisor_factory):
    """Clicking the Start-menu shortcut while JARVIS is already running is
    an ordinary way people open an app they think is closed. It must
    surface the native window — not open a browser tab, which is what it
    used to do."""
    import webbrowser

    from app.launcher import attention, gui

    sup, order, _ = supervisor_factory()
    monkeypatch.setattr(gui.instance_lock, "check_existing_instance",
                        lambda host, port: gui.instance_lock.InstanceCheckResult(True, False, pid=999))
    acquire = MagicMock()
    monkeypatch.setattr(gui.instance_lock, "acquire_lock", acquire)
    monkeypatch.setattr(gui, "LauncherSupervisor", lambda: sup)
    opened = MagicMock()
    monkeypatch.setattr(webbrowser, "open", opened)
    requested = MagicMock(return_value=True)
    monkeypatch.setattr(attention, "request", requested)

    with pytest.raises(SystemExit) as exc:
        gui.launch()

    assert exc.value.code == 0
    assert order == [], "a second launch must not create a server or window"
    acquire.assert_not_called(), "a second launch must not take the lock"
    requested.assert_called_once()
    opened.assert_not_called()


def test_startup_clears_a_stale_attention_marker(monkeypatch, supervisor_factory):
    """A marker left behind by a crashed run must not make this run's
    window pop up unbidden a moment after startup."""
    from app.launcher import attention, gui

    sup, _, _ = supervisor_factory()
    monkeypatch.setattr(gui.instance_lock, "check_existing_instance",
                        lambda host, port: gui.instance_lock.InstanceCheckResult(False, False))
    monkeypatch.setattr(gui.instance_lock, "acquire_lock", MagicMock())
    monkeypatch.setattr(gui, "LauncherSupervisor", lambda: sup)
    cleared = MagicMock()
    monkeypatch.setattr(attention, "clear", cleared)

    gui.launch()

    cleared.assert_called_once()


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

def _instrument_restart(sup, old_server, old_window, events):
    from app.launcher.window_process import WindowStartResult

    sup.start_server = lambda: (events.append("start_server"), True)[1]
    sup.start_window_detailed = lambda: (
        events.append("start_window"), WindowStartResult(True, ""),
    )[1]
    original_server_stop = old_server.stop
    original_window_stop = old_window.stop
    original_release = old_server.wait_until_port_released
    old_server.stop = lambda timeout_seconds=None: (events.append("stop_server"), original_server_stop(timeout_seconds))[1]
    old_server.wait_until_port_released = lambda timeout_seconds=None: (
        events.append("await_port_release"), original_release(timeout_seconds),
    )[1]
    old_window.stop = lambda timeout_seconds=None: (events.append("stop_window"), original_window_stop(timeout_seconds))[1]


def test_restart_stops_window_then_server_then_starts_fresh_ones(supervisor_factory):
    """Ordering matters: no child may be left pointing at a runtime that
    is going away, and the new server must never race the old one's
    socket."""
    from app.launcher import gui

    sup = gui.LauncherSupervisor()
    old_server, old_window = _FakeServer(), _FakeWindow()
    sup._server, sup._window = old_server, old_window

    events = []
    _instrument_restart(sup, old_server, old_window, events)

    assert sup.restart().ok is True
    assert events == [
        "stop_window", "stop_server", "await_port_release", "start_server", "start_window",
    ]


def test_restart_waits_for_the_port_before_starting_a_new_server():
    """A stopped process and a released socket are different events. The
    old code assumed the first implied the second and started the
    replacement server straight into a port that was still held."""
    from app.launcher import gui

    sup = gui.LauncherSupervisor()
    sup._server = _FakeServer(port_released=False)
    sup._window = _FakeWindow()
    sup.start_server = lambda: pytest.fail("must not start a server before the port is free")
    sup.start_window_detailed = lambda: pytest.fail("must not start a window either")

    result = sup.restart()

    assert result.ok is False
    assert result.stage == "port_busy"
    assert result.server_healthy is False


def test_restart_that_only_loses_the_window_is_not_reported_as_a_dead_runtime():
    """The exact defect from the hardware test: the user was told "the
    JARVIS runtime did not come back up" and to quit and relaunch — while
    the runtime was healthy and only the window had failed."""
    from app.launcher import gui, ipc
    from app.launcher.window_process import WindowStartResult

    sup = gui.LauncherSupervisor()
    sup._server, sup._window = _FakeServer(), _FakeWindow()
    sup.start_server = lambda: True
    sup.start_window_detailed = lambda: WindowStartResult(False, ipc.ERROR_WEBVIEW2_MISSING)

    result = sup.restart()

    assert result.ok is False
    assert result.stage == "window_failed"
    assert result.server_healthy is True, "the runtime came back; saying otherwise sends the user to fix nothing"
    assert result.window_reason == ipc.ERROR_WEBVIEW2_MISSING


def test_a_failed_restart_leaves_no_stale_window_handle():
    """A handle to a window that never opened would make the next stop()
    spend its whole timeout budget waiting on a process that is already
    gone."""
    from app.launcher import gui
    from app.launcher.window_process import WindowStartResult

    sup = gui.LauncherSupervisor()
    sup._server, sup._window = _FakeServer(), _FakeWindow()
    sup.start_server = lambda: True
    sup.start_window_detailed = gui.LauncherSupervisor.start_window_detailed.__get__(sup)
    sup._window_process_factory = None

    import app.launcher.window_process as wp
    original = wp.WindowProcess.start_detailed
    try:
        wp.WindowProcess.start_detailed = lambda self, base_env=None: WindowStartResult(False, "window_failed")
        assert sup.restart().ok is False
    finally:
        wp.WindowProcess.start_detailed = original

    assert sup.window is None


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
    sup.start_window_detailed = lambda: pytest.fail("must not start a window after a failed server start")

    result = sup.restart()

    assert result.ok is False
    assert result.stage == "server_unhealthy"
    assert result.server_healthy is False


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


def test_quit_runs_in_order_and_verifies_the_port_is_free(monkeypatch):
    """Window (the interface, so no new work can be submitted), then the
    server (which ends the speech runtime and any audio with it), then a
    real check that the port is free, then the lock — so the next launch
    never meets its own dying predecessor."""
    from app.launcher import gui

    events = []
    monkeypatch.setattr(gui.instance_lock, "release_lock", lambda: events.append("release_lock"))

    sup = gui.LauncherSupervisor()
    server, window = _FakeServer(), _FakeWindow()
    sup._server, sup._window = server, window

    original_server_stop = server.stop
    original_window_stop = window.stop
    original_release = server.wait_until_port_released
    server.stop = lambda timeout_seconds=None: (events.append("stop_server"), original_server_stop(timeout_seconds))[1]
    window.stop = lambda timeout_seconds=None: (events.append("stop_window"), original_window_stop(timeout_seconds))[1]
    server.wait_until_port_released = lambda timeout_seconds=None: (
        events.append("await_port_release"), original_release(timeout_seconds),
    )[1]

    sup.quit()

    assert events == ["stop_window", "stop_server", "await_port_release", "release_lock"]


def test_quit_uses_tighter_budgets_than_a_restart(monkeypatch):
    """Someone who chose Quit is watching a tray icon that has not gone
    away yet; a restart can afford to wait on a merely slow child."""
    from app.launcher import gui

    monkeypatch.setattr(gui.instance_lock, "release_lock", MagicMock())

    sup = gui.LauncherSupervisor()
    server, window = _FakeServer(), _FakeWindow()
    sup._server, sup._window = server, window
    budgets = {}
    server.stop = lambda timeout_seconds=None: budgets.setdefault("server", timeout_seconds)
    window.stop = lambda timeout_seconds=None: budgets.setdefault("window", timeout_seconds)

    sup.quit()

    assert budgets["window"] == gui.QUIT_WINDOW_STOP_TIMEOUT_SECONDS
    assert budgets["server"] == gui.QUIT_SERVER_STOP_TIMEOUT_SECONDS
    assert gui.QUIT_WINDOW_STOP_TIMEOUT_SECONDS < gui.WINDOW_STOP_TIMEOUT_SECONDS
    assert gui.QUIT_SERVER_STOP_TIMEOUT_SECONDS < gui.SERVER_STOP_TIMEOUT_SECONDS


def test_a_port_that_never_frees_still_lets_quit_finish(monkeypatch):
    """A failed child shutdown must not leave the parent stuck: the lock
    is still released and the process still ends."""
    from app.launcher import gui

    release = MagicMock()
    monkeypatch.setattr(gui.instance_lock, "release_lock", release)

    sup = gui.LauncherSupervisor()
    sup._server = _FakeServer(port_released=False)
    sup._window = _FakeWindow()

    sup.quit()

    release.assert_called_once()
    assert sup.quitting is True


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
    assert window.started is False, "a live window must be focused, not relaunched"


def test_open_or_focus_starts_a_window_when_there_is_none(monkeypatch):
    from app.launcher import gui
    from app.launcher.window_process import WindowStartResult

    sup = gui.LauncherSupervisor()
    started = {}
    monkeypatch.setattr(
        sup, "start_window_detailed",
        lambda: (started.setdefault("yes", True), WindowStartResult(True, ""))[1],
    )

    assert sup.open_or_focus_window() is True
    assert started == {"yes": True}


def test_open_or_focus_replaces_a_window_child_that_died(monkeypatch):
    """The tray's "Open JARVIS" must not be permanently broken by a
    crashed window: the server and tray are still alive, so a fresh
    window is the correct recovery."""
    from app.launcher import gui
    from app.launcher.window_process import WindowStartResult

    sup = gui.LauncherSupervisor()
    dead = _FakeWindow(running=False)
    sup._window = dead
    monkeypatch.setattr(sup, "start_window_detailed", lambda: WindowStartResult(True, ""))

    assert sup.open_or_focus_window() is True
    assert dead.stopped is True, "the dead child's control channel must be closed, not orphaned"


def test_open_or_focus_replaces_a_window_child_that_stopped_answering(monkeypatch):
    """A window process that is alive but no longer answering its control
    channel is as useless to the user as a dead one, and reporting
    success would leave "Open JARVIS" silently doing nothing."""
    from app.launcher import gui
    from app.launcher.window_process import WindowStartResult

    sup = gui.LauncherSupervisor()
    mute = _FakeWindow(running=True, shows=False)
    sup._window = mute
    replaced = {}
    monkeypatch.setattr(
        sup, "start_window_detailed",
        lambda: (replaced.setdefault("yes", True), WindowStartResult(True, ""))[1],
    )

    assert sup.open_or_focus_window() is True
    assert mute.stopped is True
    assert replaced == {"yes": True}


def test_open_or_focus_reports_why_no_window_could_be_opened(monkeypatch):
    """Every route a user takes to "open JARVIS" ends here, and each of
    them needs to be able to name a missing runtime rather than quietly
    substituting a browser tab."""
    from app.launcher import gui, ipc
    from app.launcher.window_process import WindowStartResult

    sup = gui.LauncherSupervisor()
    monkeypatch.setattr(
        sup, "start_window_detailed",
        lambda: WindowStartResult(False, ipc.ERROR_WEBVIEW2_MISSING),
    )

    result = sup.open_or_focus_window_detailed()

    assert result.ok is False
    assert result.reason == ipc.ERROR_WEBVIEW2_MISSING


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
