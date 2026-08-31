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

from app.launcher import ipc, process_tree


# See the identical constant in test_launcher_ipc.py: a timed wait can
# finish fractionally before time.monotonic() agrees its deadline passed,
# because the wait and the measurement read different clocks. The bound
# still fails a terminate that skipped the graceful window entirely.
CLOCK_SLACK_SECONDS = 0.05

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
        # healthy child: it got as far as building a window object and
        # starting its command pump. (That is all EVENT_READY proves —
        # not that anything rendered; see app/launcher/ipc.py.) None
        # models a child that connected and then reported nothing.
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
    elapsed = time.monotonic() - started
    assert elapsed >= 0.4 - CLOCK_SLACK_SECONDS, "terminate must not pre-empt the graceful window"


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

    Hands back ProcessIdentity objects rather than bare PIDs, because
    that is what the module under test now captures — see
    app/launcher/process_tree.py for why a PID alone is not enough to
    safely terminate anything.
    """

    def __init__(self, process, children):
        self._process = process
        self._children = children
        self.terminated = []

    def capture_descendants(self, pid):
        if pid is None or self._process.poll() is not None:
            return []          # dead parent: the relationship is gone
        return [
            process_tree.ProcessIdentity(pid=child, create_time=1000.0 + child, name="msedgewebview2.exe")
            for child in self._children
        ]

    def terminate_identities(self, identities, **kwargs):
        self.terminated.extend(identity.pid for identity in identities)
        return process_tree.CleanupReport()


def _with_tree_spy(monkeypatch, process, children):
    import app.launcher.window_process as wp

    spy = _TreeSpy(process, children)
    monkeypatch.setattr(wp, "capture_descendants", spy.capture_descendants)
    monkeypatch.setattr(wp, "terminate_identities", spy.terminate_identities)
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
        return spy.capture_descendants(pid)

    monkeypatch.setattr(wp, "capture_descendants", _descendants)
    monkeypatch.setattr(wp, "terminate_identities", spy.terminate_identities)
    window.start(base_env={})

    window.stop(timeout_seconds=0.3)

    assert 7 in spy.terminated, "a helper that appeared after the first capture was missed"


def test_the_last_capture_happens_after_the_final_poll_not_before(monkeypatch):
    """The gap that let cycle 2 of the lifecycle test fail.

    The graceful loop used to check poll() first and capture second, so a
    helper born in the last sleep interval before the window child exited
    was never recorded — and a process that was never captured is one
    that is never cleaned up. Capturing first closes that gap: whatever
    exists at the moment of the final poll has been seen.
    """
    import app.launcher.window_process as wp

    # Exits on the second poll, and spawns a helper just before it does.
    process = _FakeProcess()
    window, process, _, _ = _make(process=process)
    children = []
    spy = _TreeSpy(process, children)
    order = []

    def _descendants(pid):
        order.append("capture")
        children.append(7)     # a helper exists by the time we look
        return spy.capture_descendants(pid)

    def _poll_then_die():
        order.append("poll")
        return None if len(order) < 4 else 0

    monkeypatch.setattr(wp, "capture_descendants", _descendants)
    monkeypatch.setattr(wp, "terminate_identities", spy.terminate_identities)
    window.start(base_env={})
    process.poll = _poll_then_die

    window.stop(timeout_seconds=1.0)

    assert order.index("capture") < order.index("poll"), "capture must precede the poll it races"
    assert 7 in spy.terminated, "the helper alive at the final poll was not cleaned up"


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

    monkeypatch.setattr(
        wp,
        "capture_descendants",
        lambda pid: (
            [process_tree.ProcessIdentity(pid=4242, create_time=1.0), process_tree.ProcessIdentity(pid=4243, create_time=2.0)]
            if pid == process.pid
            else []
        ),
    )
    terminated = []

    def _terminate(identities, **kwargs):
        terminated.extend(identity.pid for identity in identities)
        return process_tree.CleanupReport()

    monkeypatch.setattr(wp, "terminate_identities", _terminate)

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
    monkeypatch.setattr(
        wp,
        "capture_descendants",
        lambda pid: (order.append("capture"), [process_tree.ProcessIdentity(pid=99, create_time=1.0)])[1],
    )
    monkeypatch.setattr(
        wp,
        "terminate_identities",
        lambda identities, **kwargs: (order.append("terminate"), process_tree.CleanupReport())[1],
    )
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

    monkeypatch.setattr(wp, "capture_descendants", MagicMock(side_effect=RuntimeError("psutil is unhappy")))
    window, _, _, _ = _make()
    window.start(base_env={})

    with pytest.raises(RuntimeError):
        wp.capture_descendants(1)  # the patched stand-in really does raise

    # ...and the real implementation swallows exactly that.
    monkeypatch.undo()
    assert wp.capture_descendants(None) == []
    assert wp.capture_descendants(-1) == []
    assert wp.terminate_identities([]).results == []  # must not raise


def test_shutdown_still_completes_when_cleanup_itself_fails(monkeypatch):
    """The window is already gone by this point. A launcher that cannot
    finish closing because tidying up leftovers failed is a worse outcome
    than an orphaned helper process — and, on a windowed build with no
    console, an exception here becomes a modal dialog nobody can dismiss.
    """
    from app.launcher import window_process as wp

    window, process, _, _ = _make()
    window.start(base_env={})
    monkeypatch.setattr(
        wp, "capture_descendants", lambda pid: [process_tree.ProcessIdentity(pid=99, create_time=1.0)]
    )
    monkeypatch.setattr(
        wp,
        "terminate_identities",
        MagicMock(side_effect=RuntimeError("psutil exploded mid-cleanup")),
    )

    assert window.stop(timeout_seconds=0.3) == "graceful"
    assert window.last_cleanup_report() is None


def test_stopping_twice_is_idempotent(monkeypatch):
    """Quit can be reached from the tray menu, the window's X and the
    WM_CLOSE handler. Two of them arriving is ordinary, not exceptional."""
    window, process, _, _ = _make()
    spy = _with_tree_spy(monkeypatch, process, children=[4242])
    window.start(base_env={})

    assert window.stop(timeout_seconds=0.3) == "graceful"
    assert window.stop(timeout_seconds=0.3) == "already_exited"
    assert process.terminate_calls == 0 and process.kill_calls == 0


def test_the_cleanup_report_is_kept_for_diagnosis(monkeypatch):
    """A WebView2 orphan that survives must be answerable from the return
    value, not only from a log line somebody has to find first."""
    window, process, _, _ = _make()
    _with_tree_spy(monkeypatch, process, children=[4242])
    window.start(base_env={})

    assert window.last_cleanup_report() is None, "nothing to report before stop() runs"
    window.stop(timeout_seconds=0.3)
    assert window.last_cleanup_report() is not None
