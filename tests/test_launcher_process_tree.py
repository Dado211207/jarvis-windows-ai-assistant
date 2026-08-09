"""Tests for app/launcher/process_tree.py.

Exercised against **real** child processes, not mocks. The whole point of
this module is what happens to processes that outlive their parent, and a
mocked psutil would prove nothing about that. Each test spawns short-lived
`sleep` processes through the current interpreter, so it works the same on
Linux CI and on a Windows runner.
"""

import subprocess
import sys
import time

import pytest

psutil = pytest.importorskip("psutil")

from app.launcher import process_tree  # noqa: E402


def _spawn_tree():
    """A parent that spawns one child and then waits — the shape WebView2
    produces, where the interesting processes are grandchildren of ours."""
    code = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "time.sleep(30)"
    )
    parent = subprocess.Popen([sys.executable, "-c", code])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process_tree.descendant_pids(parent.pid):
            return parent
        time.sleep(0.05)
    parent.kill()
    parent.wait(timeout=10)
    pytest.fail("the test child never spawned its own child")


def _cleanup(parent):
    try:
        process_tree.terminate_descendants_of(parent.pid)
        parent.kill()
        parent.wait(timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Finding descendants
# ---------------------------------------------------------------------------

def test_descendants_of_a_live_process_are_found():
    parent = _spawn_tree()
    try:
        assert process_tree.descendant_pids(parent.pid), "a real child must be discoverable"
    finally:
        _cleanup(parent)


def test_a_process_with_no_children_reports_none():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert process_tree.descendant_pids(process.pid) == []
    finally:
        process.kill()
        process.wait(timeout=10)


@pytest.mark.parametrize("pid", [None, -1, 0x7FFFFFFF])
def test_an_impossible_pid_is_not_an_error(pid):
    """Best-effort cleanup: tidying up leftovers must never be able to
    break shutdown."""
    assert process_tree.descendant_pids(pid) == []


def test_descendants_cannot_be_found_once_the_parent_is_gone():
    """The reason capture has to happen *before* the kill: the
    relationship that identifies our leftovers dies with the parent."""
    parent = _spawn_tree()
    captured = process_tree.descendant_pids(parent.pid)
    assert captured

    parent.kill()
    parent.wait(timeout=10)

    assert process_tree.descendant_pids(parent.pid) == []
    process_tree.terminate_pids(captured)


# ---------------------------------------------------------------------------
# Terminating them
# ---------------------------------------------------------------------------

def test_captured_descendants_are_really_terminated():
    """The reported defect: after Quit, WebView2's processes were still
    listed in Task Manager as JARVIS."""
    parent = _spawn_tree()
    captured = process_tree.descendant_pids(parent.pid)
    parent.kill()
    parent.wait(timeout=10)

    process_tree.terminate_pids(captured)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not any(psutil.pid_exists(pid) for pid in captured):
            return
        time.sleep(0.1)
    _cleanup(parent)
    pytest.fail(f"leftover processes survived cleanup: {captured}")


def test_terminating_nothing_is_a_no_op():
    process_tree.terminate_pids([])  # must not raise


def test_terminating_already_dead_pids_is_silent():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=10)

    process_tree.terminate_pids([process.pid])  # must not raise


def test_cleanup_survives_psutil_being_unavailable(monkeypatch):
    """psutil is an optional convenience here, not a dependency shutdown
    is allowed to require."""
    import builtins

    real_import = builtins.__import__

    def _no_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("no psutil in this build")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)

    assert process_tree.descendant_pids(1) == []
    process_tree.terminate_pids([1])  # must not raise
    process_tree.terminate_descendants_of(1)  # must not raise


def test_only_our_own_descendants_are_ever_targeted():
    """Never a name-based sweep: a process named msedge that the user is
    browsing in is not ours to kill. The only input is a walk down from a
    process this launcher started."""
    import inspect

    source = inspect.getsource(process_tree)

    for forbidden in ("process_iter", "name()", "taskkill", "pkill"):
        assert forbidden not in source, f"process_tree reaches for {forbidden}"
