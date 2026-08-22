"""Tests for app/launcher/instance_lock.py's single-instance detection.

psutil's liveness/name checks are monkeypatched throughout rather than
relying on the real test process's name (fragile — varies by how pytest
itself was invoked). The port-conflict checks use a real bound socket,
since that's the one part of this module that must genuinely observe OS
socket state to be worth testing at all.
"""

import json
import os
import socket

import pytest


@pytest.fixture(autouse=True)
def _isolated_app_data_root(tmp_path, monkeypatch):
    from app.launcher import instance_lock
    monkeypatch.setattr(instance_lock, "app_data_root", lambda: tmp_path)
    return tmp_path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# lock_file_path
# ---------------------------------------------------------------------------

def test_lock_file_path_under_app_data_root(tmp_path):
    from app.launcher import instance_lock
    assert instance_lock.lock_file_path() == tmp_path / "jarvis.lock"


# ---------------------------------------------------------------------------
# acquire_lock / release_lock round trip
# ---------------------------------------------------------------------------

def test_acquire_lock_writes_our_own_pid(tmp_path):
    from app.launcher import instance_lock
    instance_lock.acquire_lock()
    data = json.loads((tmp_path / "jarvis.lock").read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()


def test_release_lock_removes_the_file(tmp_path):
    from app.launcher import instance_lock
    instance_lock.acquire_lock()
    instance_lock.release_lock()
    assert not (tmp_path / "jarvis.lock").exists()


def test_release_lock_is_safe_when_no_lock_exists(tmp_path):
    from app.launcher import instance_lock
    instance_lock.release_lock()  # must not raise


# ---------------------------------------------------------------------------
# check_existing_instance — no lock file
# ---------------------------------------------------------------------------

def test_no_lock_no_port_conflict_reports_all_clear():
    from app.launcher import instance_lock
    result = instance_lock.check_existing_instance("127.0.0.1", _free_port())
    assert result.another_instance_running is False
    assert result.port_in_use_by_other is False


def test_no_lock_but_port_bound_reports_conflict():
    from app.launcher import instance_lock
    port = _free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        result = instance_lock.check_existing_instance("127.0.0.1", port)
    assert result.another_instance_running is False
    assert result.port_in_use_by_other is True


# ---------------------------------------------------------------------------
# check_existing_instance — lock file present
# ---------------------------------------------------------------------------

def test_live_jarvis_lock_wins_without_checking_the_port(tmp_path, monkeypatch):
    from app.launcher import instance_lock
    (tmp_path / "jarvis.lock").write_text(json.dumps({"pid": 4242}), encoding="utf-8")
    monkeypatch.setattr(instance_lock.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(instance_lock, "_process_looks_like_jarvis", lambda pid: True)

    # Even a bound port must not flip this to "conflict" — a live JARVIS
    # lock is authoritative and short-circuits the port check entirely.
    port = _free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        result = instance_lock.check_existing_instance("127.0.0.1", port)

    assert result.another_instance_running is True
    assert result.pid == 4242
    assert result.port_in_use_by_other is False


def test_dead_pid_lock_is_cleared_and_falls_through_to_port_check(tmp_path, monkeypatch):
    from app.launcher import instance_lock
    (tmp_path / "jarvis.lock").write_text(json.dumps({"pid": 99999}), encoding="utf-8")
    monkeypatch.setattr(instance_lock.psutil, "pid_exists", lambda pid: False)

    result = instance_lock.check_existing_instance("127.0.0.1", _free_port())

    assert result.another_instance_running is False
    assert not (tmp_path / "jarvis.lock").exists()


def test_live_but_unrelated_process_reusing_pid_is_treated_as_stale(tmp_path, monkeypatch):
    """The recorded PID is alive, but it's not a JARVIS-looking process
    (classic PID-reuse-after-crash scenario) — must not be trusted."""
    from app.launcher import instance_lock
    (tmp_path / "jarvis.lock").write_text(json.dumps({"pid": 4242}), encoding="utf-8")
    monkeypatch.setattr(instance_lock.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(instance_lock, "_process_looks_like_jarvis", lambda pid: False)

    result = instance_lock.check_existing_instance("127.0.0.1", _free_port())

    assert result.another_instance_running is False
    assert not (tmp_path / "jarvis.lock").exists()


def test_corrupted_lock_file_is_treated_as_stale(tmp_path):
    from app.launcher import instance_lock
    (tmp_path / "jarvis.lock").write_text("not valid json{{{", encoding="utf-8")

    result = instance_lock.check_existing_instance("127.0.0.1", _free_port())

    assert result.another_instance_running is False
    assert not (tmp_path / "jarvis.lock").exists()


# ---------------------------------------------------------------------------
# _process_looks_like_jarvis
# ---------------------------------------------------------------------------

def test_process_looks_like_jarvis_matches_packaged_exe_name(monkeypatch):
    from app.launcher import instance_lock

    class _FakeProcess:
        def name(self):
            return "JARVIS.exe"

    monkeypatch.setattr(instance_lock.psutil, "Process", lambda pid: _FakeProcess())
    assert instance_lock._process_looks_like_jarvis(1234) is True


def test_process_looks_like_jarvis_false_for_unrelated_name(monkeypatch):
    from app.launcher import instance_lock

    class _FakeProcess:
        def name(self):
            return "notepad.exe"

    monkeypatch.setattr(instance_lock.psutil, "Process", lambda pid: _FakeProcess())
    assert instance_lock._process_looks_like_jarvis(1234) is False


def test_process_looks_like_jarvis_false_when_process_vanished(monkeypatch):
    import psutil as real_psutil
    from app.launcher import instance_lock

    def _raise(pid):
        raise real_psutil.NoSuchProcess(pid)

    monkeypatch.setattr(instance_lock.psutil, "Process", _raise)
    assert instance_lock._process_looks_like_jarvis(1234) is False
