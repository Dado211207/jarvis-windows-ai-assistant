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
    def __init__(self, secret, accept_result=True):
        self.secret = secret
        self.sent = []
        self.closed = False
        self._accept_result = accept_result
        self.events = []
        self.owner = None

    def address_string(self):
        return "127.0.0.1:65000"

    def accept(self):
        return self._accept_result

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


def _make(url="http://127.0.0.1:5555/ui/", process=None, accept=True, close_action="tray"):
    from app.launcher.window_process import WindowProcess

    process = process or _FakeProcess()
    listeners = []

    def _listener_factory(secret):
        listener = _FakeListener(secret, accept_result=accept)
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


def test_stop_on_an_already_exited_child_does_not_signal_it():
    window, process, _, _ = _make()
    window.start(base_env={})
    process.die()

    assert window.stop() == "already_exited"
    assert process.terminate_calls == 0
    assert process.kill_calls == 0
