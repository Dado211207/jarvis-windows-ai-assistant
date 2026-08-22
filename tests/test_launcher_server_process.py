"""Tests for app/launcher/server_process.py.

The child process is exercised through an injected *spawn* seam with a
fake process object, so every lifecycle branch (crash before health,
unexpected exit after startup, graceful stop, forced kill after timeout)
is provable on this repo's Linux CI without spawning a real server per
case. The health-wait itself is additionally proven end to end against a
real running server in test_health_wait_succeeds_against_a_real_server.
"""

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeProcess:
    """Stand-in for subprocess.Popen with controllable exit behaviour."""

    def __init__(self, exit_after: float = None, exit_code: int = 0, ignore_terminate: bool = False):
        self.pid = 4242
        self.stdout = None
        self._exit_at = (time.monotonic() + exit_after) if exit_after is not None else None
        self._exit_code = exit_code
        self._ignore_terminate = ignore_terminate
        self.terminate_calls = 0
        self.kill_calls = 0
        self._forced_exit = False

    def poll(self):
        if self._forced_exit:
            return self._exit_code
        if self._exit_at is not None and time.monotonic() >= self._exit_at:
            return self._exit_code
        return None

    def terminate(self):
        self.terminate_calls += 1
        if not self._ignore_terminate:
            self._forced_exit = True

    def kill(self):
        self.kill_calls += 1
        self._forced_exit = True


class _SpawnRecorder:
    def __init__(self, process=None):
        self.calls = []
        self._process = process or _FakeProcess()

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        return self._process


@pytest.fixture
def isolated_logs(tmp_path, monkeypatch):
    """Keeps every child-log write inside tmp_path — these tests must
    never append to the developer's real data/logs/ directory."""
    monkeypatch.setattr("app.core.app_paths.logs_dir", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Redaction — the session secret must never reach a log file
# ---------------------------------------------------------------------------

def test_redact_text_removes_an_api_key_shape():
    from app.launcher.server_process import redact_text
    assert "sk-abcdef1234567890" not in redact_text("using sk-abcdef1234567890 now")


def test_redact_text_removes_keyed_secrets():
    from app.launcher.server_process import redact_text
    for line in ("api_key: hunter2", "TOKEN=abc123", "password = letmein"):
        assert "hunter2" not in redact_text(line)
        assert "abc123" not in redact_text(line)
        assert "letmein" not in redact_text(line)


def test_redact_text_removes_the_literal_session_secret():
    """A generated secret has no recognisable shape, so pattern matching
    alone could never catch it — matching its literal value is the only
    reliable way to keep it out of the logs."""
    from app.launcher.server_process import redact_text
    secret = "Zm9vYmFyLXNlY3JldA"
    assert secret not in redact_text(f"starting with {secret} ok", [secret])


def test_generated_session_secrets_are_unique_and_substantial():
    from app.launcher.server_process import generate_session_secret
    first, second = generate_session_secret(), generate_session_secret()
    assert first != second
    assert len(first) >= 32


# ---------------------------------------------------------------------------
# Command construction — the child must not need a system Python
# ---------------------------------------------------------------------------

def test_frozen_build_reinvokes_the_packaged_executable_with_api():
    from app.launcher.server_process import build_command
    assert build_command(executable=r"C:\Program Files\JARVIS\JARVIS.exe", frozen=True) == [
        r"C:\Program Files\JARVIS\JARVIS.exe", "--api",
    ]


def test_dev_build_runs_run_jarvis_through_the_interpreter():
    from app.launcher.server_process import build_command
    command = build_command(executable="/usr/bin/python3", frozen=False)
    assert command[0] == "/usr/bin/python3"
    assert command[1].endswith("run_jarvis.py")
    assert command[2] == "--api"


# ---------------------------------------------------------------------------
# Environment — loopback + secret passed safely
# ---------------------------------------------------------------------------

def test_environment_carries_loopback_port_and_secret():
    from app.launcher.server_process import SESSION_SECRET_ENV, ServerProcess
    server = ServerProcess(host="127.0.0.1", port=5555, session_secret="s3cr3t")
    env = server.environment(base_env={"EXISTING": "kept"})
    assert env["EXISTING"] == "kept"
    assert env["JARVIS_HOST"] == "127.0.0.1"
    assert env["JARVIS_PORT"] == "5555"
    assert env[SESSION_SECRET_ENV] == "s3cr3t"


def test_secret_is_never_placed_on_the_command_line(isolated_logs):
    """A Windows command line is readable by other processes (WMI,
    NtQueryInformationProcess); an environment block is not exposed the
    same way. Checks the command actually handed to spawn(), not just
    build_command() in isolation."""
    from app.launcher.server_process import ServerProcess

    recorder = _SpawnRecorder()
    server = ServerProcess(
        host="127.0.0.1", port=_free_port(), session_secret="s3cr3t", spawn=recorder,
    )
    server.start()

    command = recorder.calls[0]["command"]
    assert not any("s3cr3t" in str(part) for part in command)
    # ...and it really is being delivered, just by the safer channel.
    assert recorder.calls[0]["env"]["JARVIS_SESSION_SECRET"] == "s3cr3t"


def test_start_refuses_a_non_loopback_host(isolated_logs):
    from app.launcher.server_process import ServerProcess
    server = ServerProcess(host="0.0.0.0", port=5555, spawn=_SpawnRecorder())
    with pytest.raises(ValueError, match="loopback"):
        server.start()


# ---------------------------------------------------------------------------
# Exactly one child
# ---------------------------------------------------------------------------

def test_start_launches_exactly_one_child(isolated_logs):
    from app.launcher.server_process import ServerProcess
    recorder = _SpawnRecorder()
    server = ServerProcess(host="127.0.0.1", port=_free_port(), spawn=recorder)

    server.start()

    assert len(recorder.calls) == 1


def test_start_uses_no_window_creation_flag_on_windows(isolated_logs, monkeypatch):
    """The packaged build must never flash a console window."""
    import app.launcher.server_process as sp
    monkeypatch.setattr(sp.sys, "platform", "win32", raising=False)
    recorder = _SpawnRecorder()
    server = sp.ServerProcess(host="127.0.0.1", port=_free_port(), spawn=recorder)

    server.start()

    assert recorder.calls[0]["creationflags"] == 0x08000000


def test_creation_flags_are_zero_off_windows(monkeypatch):
    import app.launcher.server_process as sp
    monkeypatch.setattr(sp.sys, "platform", "linux", raising=False)
    assert sp._creation_flags() == 0


# ---------------------------------------------------------------------------
# Health wait
# ---------------------------------------------------------------------------

def test_health_wait_fails_fast_when_child_crashes_before_health(isolated_logs):
    """A crash during startup must be reported as a crash, not silently
    absorbed into the full timeout — the difference is what makes a
    startup failure diagnosable."""
    from app.launcher.server_process import ServerProcess
    crashed = _FakeProcess(exit_after=0.0, exit_code=3)
    server = ServerProcess(host="127.0.0.1", port=_free_port(), spawn=_SpawnRecorder(crashed))
    server.start()

    started = time.monotonic()
    assert server.wait_until_healthy(timeout_seconds=10) is False
    assert time.monotonic() - started < 5, "must not burn the whole timeout on an already-dead child"
    assert server.returncode == 3


def test_health_wait_times_out_when_child_never_serves(isolated_logs):
    from app.launcher.server_process import ServerProcess
    alive_but_silent = _FakeProcess()
    server = ServerProcess(host="127.0.0.1", port=_free_port(), spawn=_SpawnRecorder(alive_but_silent))
    server.start()

    assert server.wait_until_healthy(timeout_seconds=1.0) is False


def test_health_wait_succeeds_against_a_real_server(isolated_logs):
    """Not a fake: a real uvicorn instance on a real port, proving the
    health URL and polling logic actually match what the app serves."""
    from app.launcher import server_runner
    from app.launcher.server_process import ServerProcess

    port = _free_port()
    running = server_runner.start_server_in_background(host="127.0.0.1", port=port)
    try:
        server = ServerProcess(host="127.0.0.1", port=port, spawn=_SpawnRecorder())
        server.start()
        assert server.wait_until_healthy(timeout_seconds=15) is True
    finally:
        running.request_shutdown(join_timeout=5)


def test_unexpected_exit_after_startup_is_observable(isolated_logs):
    from app.launcher.server_process import ServerProcess
    process = _FakeProcess()
    server = ServerProcess(host="127.0.0.1", port=_free_port(), spawn=_SpawnRecorder(process))
    server.start()
    assert server.is_running() is True

    process._forced_exit = True  # the child dies on its own, unprompted

    assert server.is_running() is False
    assert server.returncode == 0


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

def test_stop_on_a_never_started_server_is_safe():
    from app.launcher.server_process import ServerProcess
    assert ServerProcess(host="127.0.0.1", port=5555).stop() == "not_started"


def test_stop_reports_already_exited(isolated_logs):
    from app.launcher.server_process import ServerProcess
    process = _FakeProcess(exit_after=0.0)
    server = ServerProcess(host="127.0.0.1", port=_free_port(), spawn=_SpawnRecorder(process))
    server.start()

    assert server.stop() == "already_exited"
    assert process.terminate_calls == 0, "a dead child must never be signalled again"


def test_stop_terminates_gracefully(isolated_logs):
    from app.launcher.server_process import ServerProcess
    process = _FakeProcess()
    server = ServerProcess(host="127.0.0.1", port=_free_port(), spawn=_SpawnRecorder(process))
    server.start()

    assert server.stop(timeout_seconds=5) == "graceful"
    assert process.terminate_calls == 1
    assert process.kill_calls == 0, "a child that stopped on request must never be killed"


def test_stop_kills_only_after_the_bounded_timeout(isolated_logs):
    """Forced termination is an escalation, never the first move."""
    from app.launcher.server_process import ServerProcess
    stubborn = _FakeProcess(ignore_terminate=True)
    server = ServerProcess(host="127.0.0.1", port=_free_port(), spawn=_SpawnRecorder(stubborn))
    server.start()

    started = time.monotonic()
    assert server.stop(timeout_seconds=0.5) == "killed"
    elapsed = time.monotonic() - started

    assert stubborn.terminate_calls == 1, "terminate must be tried before kill"
    assert stubborn.kill_calls == 1
    assert elapsed >= 0.5, "kill must not pre-empt the graceful window"


def test_no_process_is_signalled_that_this_instance_did_not_start():
    """The owned-child guarantee: with nothing started, stop() must not
    reach for a PID at all."""
    from app.launcher.server_process import ServerProcess
    server = ServerProcess(host="127.0.0.1", port=5555)
    assert server.pid is None
    assert server.stop() == "not_started"


# ---------------------------------------------------------------------------
# Child log file
# ---------------------------------------------------------------------------

def test_child_log_lives_under_the_jarvis_logs_directory(isolated_logs):
    from app.launcher.server_process import child_log_path
    assert child_log_path().parent == isolated_logs
    assert child_log_path().name == "jarvis-server.log"


def test_log_pump_redacts_before_writing(isolated_logs):
    """End-to-end proof for requirement 7: a secret emitted by the child
    never lands in the log file on disk."""
    import io

    from app.launcher.server_process import ServerProcess

    secret = "TOPSECRETVALUE123"

    class _ProcessWithOutput(_FakeProcess):
        def __init__(self):
            super().__init__()
            self.stdout = io.BytesIO(
                f"starting\nkey={secret}\nusing sk-deadbeefcafe1234\ndone\n".encode()
            )

    process = _ProcessWithOutput()
    server = ServerProcess(
        host="127.0.0.1", port=_free_port(), session_secret=secret, spawn=_SpawnRecorder(process),
    )
    server.start()
    server._pump_thread.join(timeout=5)

    written = (isolated_logs / "jarvis-server.log").read_text(encoding="utf-8")
    assert "starting" in written and "done" in written, "ordinary output must still be logged"
    assert secret not in written
    assert "sk-deadbeefcafe1234" not in written
    assert "[REDACTED]" in written


def test_log_rotation_keeps_files_bounded(isolated_logs):
    from app.launcher import server_process

    log_path = isolated_logs / "jarvis-server.log"
    log_path.write_text("x" * (server_process.CHILD_LOG_MAX_BYTES + 1), encoding="utf-8")

    server_process._rotate_if_needed(log_path)

    assert not log_path.exists(), "the oversized file must have been rolled aside"
    assert log_path.with_suffix(".log.1").exists()


# ---------------------------------------------------------------------------
# Port release — a stopped process and a released socket are not the same
# event
# ---------------------------------------------------------------------------

def test_a_free_port_is_reported_free():
    from app.launcher.server_process import ServerProcess

    server = ServerProcess(host="127.0.0.1", port=_free_port())

    assert server.port_is_free() is True


def test_a_port_with_a_live_listener_is_never_reported_free():
    """The check that has to hold on Windows, where SO_REUSEADDR lets a
    bind succeed while another process is still listening — a bind-only
    probe would call this port free and send the replacement server
    straight into a collision."""
    from app.launcher.server_process import ServerProcess

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        server = ServerProcess(host="127.0.0.1", port=port)
        assert server.port_is_free() is False
    finally:
        listener.close()


def test_wait_until_port_released_returns_as_soon_as_the_listener_goes_away():
    import threading as _threading

    from app.launcher.server_process import ServerProcess

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    server = ServerProcess(host="127.0.0.1", port=port)

    _threading.Timer(0.3, listener.close).start()

    started = time.monotonic()
    assert server.wait_until_port_released(timeout_seconds=10) is True
    assert time.monotonic() - started < 5.0


def test_wait_until_port_released_gives_up_and_says_so():
    """A port held by something else must fail the wait rather than block
    a restart forever — and the restart then reports "port_busy" instead
    of blaming the runtime."""
    from app.launcher.server_process import ServerProcess

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        server = ServerProcess(host="127.0.0.1", port=port)
        started = time.monotonic()
        assert server.wait_until_port_released(timeout_seconds=0.5) is False
        assert 0.5 <= time.monotonic() - started < 5.0
    finally:
        listener.close()


def test_stop_waits_for_the_kill_to_land_before_reporting_it():
    """Returning "killed" while the process is still alive is how a
    restart ends up racing a port that was never released."""
    from app.launcher.server_process import ServerProcess

    process = _FakeProcess(ignore_terminate=True)
    server = ServerProcess(host="127.0.0.1", port=_free_port(), spawn=_SpawnRecorder(process))
    server.start()

    assert server.stop(timeout_seconds=0.3) == "killed"
    assert process.terminate_calls == 1, "terminate must be tried before kill"
    assert process.kill_calls == 1
    assert process.poll() is not None, "stop() must not return before the child is really gone"


def test_the_probe_does_not_set_so_reuseaddr_on_windows(monkeypatch):
    """The bug this pins, which took two CI rounds to find.

    SO_REUSEADDR means opposite things on the two platforms: on Windows it
    lets a socket bind a port another socket is actively listening on, so
    a probe that sets it reports every busy port as free. On POSIX it only
    permits rebinding through TIME_WAIT, and uvicorn sets it, so a probe
    without it is stricter than the real server.

    Asserted by watching the option actually being set, because the
    consequence — a restart racing a port that was never released — is
    invisible on the Linux machine this suite usually runs on.
    """
    import socket as socket_module

    from app.launcher import server_process as sp
    from app.launcher.server_process import ServerProcess

    options = []
    real_socket = socket_module.socket

    class _WatchingSocket(real_socket):
        def setsockopt(self, level, option, value):
            options.append((level, option, value))
            return super().setsockopt(level, option, value)

    monkeypatch.setattr(socket_module, "socket", _WatchingSocket)

    server = ServerProcess(host="127.0.0.1", port=_free_port())

    monkeypatch.setattr(sp.sys, "platform", "win32")
    options.clear()
    server.port_is_free()
    assert (socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1) not in options, (
        "on Windows this would report every busy port as free"
    )

    monkeypatch.setattr(sp.sys, "platform", "linux")
    options.clear()
    server.port_is_free()
    assert (socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1) in options, (
        "on POSIX the probe must match what uvicorn itself does"
    )
