"""Tests for app/launcher/window_process.py — the parent-side supervisor
for the window child.

Both the process spawn and the IPC listener are injected, so window
lifecycle (start, never-two, crash-then-relaunch, graceful close,
escalation to terminate/kill) is provable without a real WebView2
desktop.
"""

import time
from unittest.mock import MagicMock

import pytest

from app.launcher import ipc

_UNSET = object()


class _FakeProcess:
    def __init__(self, ignore_quit=False, ignore_terminate=False):
        self.pid = 7777
        self._exited = False
        self._ignore_terminate = ignore_terminate
        self.ignore_quit = ignore_quit
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return 0 if self._exited else None

    def terminate(self):
        self.terminate_calls += 1
        if not self._ignore_terminate:
            self._exited = True

    def kill(self):
        self.kill_calls += 1
        self._exited = True

    def die(self):
        self._exited = True


class _FakeListener:
    def __init__(self, secret, accept_result=True, ready_event=_UNSET):
        self.secret = secret
        self.sent = []
        self.closed = False
        self._accept_result = accept_result
        # What the child reports once connected. The default models a
        # healthy child: a real window appeared. None models a child that
        # connected and then never showed one.
        self._ready_event = {"event": ipc.EVENT_READY} if ready_event is _UNSET else ready_event
        self.accept_timeouts = []
        self.wait_timeouts = []
        self.events = []
        self.owner = None

    def address_string(self):
        return "127.0.0.1:65000"

    def accept(self, timeout_seconds=None):
        self.accept_timeouts.append(timeout_seconds)
        return self._accept_result

    def wait_for_event(self, name, timeout_seconds):
        self.wait_timeouts.append(timeout_seconds)
        return self._ready_event

    def send_command(self, command):
        self.sent.append(command)
        # A real window child exits when told to quit; model that so the
        # graceful path is exercised rather than assumed.
        if command == ipc.COMMAND_QUIT and self.owner is not None and not self.owner.ignore_quit:
            self.owner.die()
        return True

    def poll_event(self, timeout=0.0):
        return self.events.pop(0) if self.events else None

    def close(self):
        self.closed = True


def _make(url="http://127.0.0.1:5555/ui/", process=None, accept=True, close_action="tray",
          ready_event=_UNSET):
    from app.launcher.window_process import WindowProcess

    process = process or _FakeProcess()
    listeners = []

    def _listener_factory(secret):
        listener = _FakeListener(secret, accept_result=accept, ready_event=ready_event)
        listener.owner = process
        listeners.append(listener)
        return listener

    spawn_calls = []

    def _spawn(command, **kwargs):
        spawn_calls.append({"command": command, **kwargs})
        return process

    window = WindowProcess(
        url=url, close_action=close_action, spawn=_spawn, listener_factory=_listener_factory,
    )
    return window, process, spawn_calls, listeners


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------

def test_frozen_build_reinvokes_the_packaged_executable_with_window():
    from app.launcher.window_process import build_command
    assert build_command(executable=r"C:\JARVIS\JARVIS.exe", frozen=True) == [r"C:\JARVIS\JARVIS.exe", "--window"]


def test_dev_build_runs_run_jarvis_with_window():
    from app.launcher.window_process import build_command
    command = build_command(executable="/usr/bin/python3", frozen=False)
    assert command[0] == "/usr/bin/python3"
    assert command[1].endswith("run_jarvis.py")
    assert command[2] == "--window"


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def test_start_launches_exactly_one_child_and_authenticates():
    window, _, spawn_calls, listeners = _make()

    assert window.start(base_env={}) is True

    assert len(spawn_calls) == 1
    assert len(listeners) == 1


def test_start_passes_ipc_context_by_environment_never_argv():
    window, _, spawn_calls, _ = _make()
    window.start(base_env={})

    call = spawn_calls[0]
    env = call["env"]
    assert env[ipc.IPC_ADDRESS_ENV] == "127.0.0.1:65000"
    assert env[ipc.IPC_URL_ENV] == "http://127.0.0.1:5555/ui/"
    assert env[ipc.IPC_SECRET_ENV]
    # The secret must not be discoverable from the command line.
    assert not any(env[ipc.IPC_SECRET_ENV] in str(part) for part in call["command"])


def test_start_uses_no_window_creation_flag_on_windows(monkeypatch):
    import app.launcher.server_process as sp
    monkeypatch.setattr(sp.sys, "platform", "win32", raising=False)
    window, _, spawn_calls, _ = _make()

    window.start(base_env={})

    assert spawn_calls[0]["creationflags"] == 0x08000000


def test_start_returns_false_and_stops_the_child_when_authentication_fails():
    window, process, _, _ = _make(accept=False)

    assert window.start(base_env={}) is False
    assert process.terminate_calls + process.kill_calls > 0 or process.poll() is not None


def test_start_never_creates_a_second_window_child():
    window, _, spawn_calls, _ = _make()
    window.start(base_env={})

    assert window.start(base_env={}) is True  # already running: a no-op success
    assert len(spawn_calls) == 1, "a second window child must never be created"


def test_a_child_that_connects_but_never_shows_a_window_is_a_failure():
    """The regression behind "the app opens a browser instead of a
    window": the parent treated the control connection itself as proof a
    window existed, so a machine that could never create one reported a
    healthy start and showed nothing."""
    window, process, _, _ = _make(ready_event=None)

    result = window.start_detailed(base_env={})

    assert result.ok is False
    assert result.reason == ipc.ERROR_WINDOW_FAILED
    assert process.poll() is not None, "a child that never showed a window must be stopped"


def test_start_reports_the_childs_own_reason_for_failing():
    """A missing WebView2 runtime is a fixable problem with a download
    link. It must survive the trip from the child to the parent intact,
    not be flattened into a generic failure."""
    window, _, _, _ = _make(
        ready_event={"event": ipc.EVENT_ERROR, "detail": ipc.ERROR_WEBVIEW2_MISSING}
    )

    result = window.start_detailed(base_env={})

    assert result.ok is False
    assert result.reason == ipc.ERROR_WEBVIEW2_MISSING


def test_connecting_and_showing_a_window_get_separate_time_budgets():
    """Connecting happens before any GUI toolkit is touched, so a long
    wait there means the process never really started; showing a window
    is legitimately slow on a cold WebView2. One shared budget cannot be
    right for both."""
    from app.launcher import window_process as wp

    window, _, _, listeners = _make()
    window.start(base_env={})

    assert listeners[0].accept_timeouts == [wp.DEFAULT_CONNECT_TIMEOUT_SECONDS]
    assert listeners[0].wait_timeouts == [wp.DEFAULT_READY_TIMEOUT_SECONDS]
    assert wp.DEFAULT_CONNECT_TIMEOUT_SECONDS < wp.DEFAULT_READY_TIMEOUT_SECONDS


def test_start_survives_a_spawn_failure_without_raising():
    from app.launcher.window_process import WindowProcess

    def _boom(*args, **kwargs):
        raise OSError("no exec for you")

    window = WindowProcess(
        url="http://x/", spawn=_boom, listener_factory=lambda secret: _FakeListener(secret),
    )
    assert window.start(base_env={}) is False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def test_show_hide_focus_reload_are_sent_over_ipc():
    window, _, _, listeners = _make()
    window.start(base_env={})

    window.show(); window.hide(); window.focus(); window.reload()

    assert listeners[0].sent == [
        ipc.COMMAND_SHOW, ipc.COMMAND_HIDE, ipc.COMMAND_FOCUS, ipc.COMMAND_RELOAD,
    ]


def test_commands_are_not_sent_to_a_dead_child():
    window, process, _, listeners = _make()
    window.start(base_env={})
    process.die()

    assert window.show() is False
    assert listeners[0].sent == []


# ---------------------------------------------------------------------------
# Crash recovery — "Open JARVIS" after the window died
# ---------------------------------------------------------------------------

def test_show_or_restart_focuses_a_live_window():
    window, _, spawn_calls, listeners = _make()
    window.start(base_env={})

    assert window.show_or_restart(base_env={}) is True
    assert len(spawn_calls) == 1, "a live window must be shown, not relaunched"
    assert listeners[0].sent == [ipc.COMMAND_SHOW]


def test_show_or_restart_relaunches_after_a_window_crash():
    """A crashed window must not leave 'Open JARVIS' permanently broken —
    the server and tray are still alive, so a fresh window is the correct
    recovery."""
    window, process, spawn_calls, _ = _make()
    window.start(base_env={})
    process.die()  # the window child crashes

    assert window.show_or_restart(base_env={}) is True
    assert len(spawn_calls) == 2, "a dead window child must be replaced"


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

def test_stop_before_start_is_safe():
    window, _, _, _ = _make()
    assert window.stop() == "not_started"


def test_stop_asks_the_window_to_close_first():
    window, _, _, listeners = _make()
    window.start(base_env={})

    assert window.stop(timeout_seconds=5) == "graceful"
    assert listeners[0].sent[-1] == ipc.COMMAND_QUIT
    assert listeners[0].closed is True


def test_stop_escalates_to_terminate_when_the_window_ignores_quit():
    window, process, _, _ = _make(process=_FakeProcess(ignore_quit=True))
    window.start(base_env={})

    started = time.monotonic()
    assert window.stop(timeout_seconds=0.4) == "terminated"

    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert time.monotonic() - started >= 0.4, "terminate must not pre-empt the graceful window"


def test_stop_kills_only_after_terminate_also_fails():
    window, process, _, _ = _make(
        process=_FakeProcess(ignore_quit=True, ignore_terminate=True)
    )
    window.start(base_env={})

    assert window.stop(timeout_seconds=0.3) == "killed"
    assert process.terminate_calls == 1, "terminate must be tried before kill"
    assert process.kill_calls == 1


# ---------------------------------------------------------------------------
# WebView2 leftovers — the reported "Quit leaves processes running" defect
# ---------------------------------------------------------------------------

class _TreeSpy:
    """Stands in for the real process tree.

    Models the property that made the bug invisible: a process's children
    are only discoverable while it is alive. Ask after it exits and you
    get nothing back — which is exactly what the old code did, and why it
    cleaned up nothing on the path everyone actually takes.
    """

    def __init__(self, process, children):
        self._process = process
        self._children = children
        self.terminated = []

    def descendant_pids(self, pid):
        if pid is None or self._process.poll() is not None:
            return []          # dead parent: the relationship is gone
        return list(self._children)

    def terminate_pids(self, pids):
        self.terminated.extend(pids)


def _with_tree_spy(monkeypatch, process, children):
    import app.launcher.window_process as wp

    spy = _TreeSpy(process, children)
    monkeypatch.setattr(wp, "descendant_pids", spy.descendant_pids)
    monkeypatch.setattr(wp, "terminate_pids", spy.terminate_pids)
    return spy


def test_a_graceful_quit_cleans_up_the_webview_processes(monkeypatch):
    """The defect, exactly: choosing Quit worked, the window closed, and
    an msedgewebview2.exe stayed in Task Manager afterwards.

    The old code asked for the child's descendants *after* confirming it
    had exited, so it walked a dead PID and found none. Descendants have
    to be captured while the child is still alive.
    """
    window, process, _, _ = _make()
    spy = _with_tree_spy(monkeypatch, process, children=[4242])
    window.start(base_env={})

    assert window.stop(timeout_seconds=5) == "graceful"
    assert spy.terminated == [4242], "a graceful quit must not leave WebView2 processes behind"


def test_leftovers_are_cleaned_up_on_every_exit_path(monkeypatch):
    """Not just the kill path, which was the only one that got it right."""
    for ignore_quit, ignore_terminate, expected in (
        (False, False, "graceful"),
        (True, False, "terminated"),
        (True, True, "killed"),
    ):
        process = _FakeProcess(ignore_quit=ignore_quit, ignore_terminate=ignore_terminate)
        window, process, _, _ = _make(process=process)
        spy = _with_tree_spy(monkeypatch, process, children=[99])
        window.start(base_env={})

        assert window.stop(timeout_seconds=0.3) == expected
        assert 99 in spy.terminated, f"leftovers survived the {expected!r} path"


def test_a_helper_process_that_appears_late_is_still_cleaned_up(monkeypatch):
    """WebView2 starts its helpers lazily, so one can appear after the
    first capture and before the window finishes closing."""
    import app.launcher.window_process as wp

    window, process, _, _ = _make(process=_FakeProcess(ignore_quit=True))
    appeared = []
    spy = _TreeSpy(process, children=appeared)

    calls = {"n": 0}

    def _descendants(pid):
        calls["n"] += 1
        if calls["n"] == 2:      # a helper shows up mid-shutdown
            appeared.append(7)
        return spy.descendant_pids(pid)

    monkeypatch.setattr(wp, "descendant_pids", _descendants)
    monkeypatch.setattr(wp, "terminate_pids", spy.terminate_pids)
    window.start(base_env={})

    window.stop(timeout_seconds=0.3)

    assert 7 in spy.terminated, "a helper that appeared after the first capture was missed"


def test_stop_on_an_already_exited_child_does_not_signal_it():
    window, process, _, _ = _make()
    window.start(base_env={})
    process.die()

    assert window.stop() == "already_exited"
    assert process.terminate_calls == 0
    assert process.kill_calls == 0


def test_stop_cleans_up_webview2_processes_the_child_left_behind(monkeypatch):
    """WebView2 hosts its browser in processes that outlive the window
    child. Killed and not cleaned up, they are what a user sees in Task
    Manager as "JARVIS is still running" after Quit."""
    from app.launcher import window_process as wp

    window, process, _, _ = _make(process=_FakeProcess(ignore_quit=True, ignore_terminate=True))
    window.start(base_env={})

    monkeypatch.setattr(wp, "descendant_pids", lambda pid: [4242, 4243] if pid == process.pid else [])
    terminated = []
    monkeypatch.setattr(wp, "terminate_pids", terminated.extend)

    assert window.stop(timeout_seconds=0.3) == "killed"
    assert terminated == [4242, 4243]


def test_descendants_are_captured_before_the_kill_not_after(monkeypatch):
    """Once the parent is gone the parent/child relationship that
    identifies its WebView2 processes is gone too, so the capture has to
    happen while it is still alive."""
    from app.launcher import window_process as wp

    window, process, _, _ = _make(process=_FakeProcess(ignore_quit=True, ignore_terminate=True))
    window.start(base_env={})

    order = []
    monkeypatch.setattr(wp, "descendant_pids", lambda pid: (order.append("capture"), [99])[1])
    monkeypatch.setattr(wp, "terminate_pids", lambda pids: order.append("terminate"))
    original_kill = process.kill

    def _kill():
        order.append("kill")
        original_kill()

    process.kill = _kill

    window.stop(timeout_seconds=0.3)

    assert order.index("capture") < order.index("kill")


def test_descendant_lookup_never_breaks_shutdown(monkeypatch):
    """Best-effort cleanup only: psutil may be absent, or the process may
    have vanished mid-lookup. Neither may propagate."""
    from app.launcher import window_process as wp

    monkeypatch.setattr(wp, "descendant_pids", MagicMock(side_effect=RuntimeError("psutil is unhappy")))
    window, _, _, _ = _make()
    window.start(base_env={})

    with pytest.raises(RuntimeError):
        wp.descendant_pids(1)  # the patched stand-in really does raise

    # ...and the real implementation swallows exactly that.
    monkeypatch.undo()
    assert wp.descendant_pids(None) == []
    assert wp.descendant_pids(-1) == []
    wp.terminate_pids([])  # must not raise
