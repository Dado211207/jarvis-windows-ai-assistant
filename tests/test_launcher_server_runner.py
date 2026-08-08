"""Tests for app/launcher/server_runner.py — real uvicorn on a real
ephemeral port, the same "actually bind and poll /health" approach
scripts/ci_windows_smoke.py and tests/test_playwright_e2e.py's server
fixture already use, not a mocked HTTP layer.
"""

import socket
import sys
from unittest.mock import patch

import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_server():
    from app.launcher import server_runner
    port = _free_port()
    running = server_runner.start_server_in_background(host="127.0.0.1", port=port)
    yield running, port
    running.request_shutdown()


def test_wait_until_healthy_true_once_server_is_up(running_server):
    from app.launcher import server_runner
    running, port = running_server
    assert server_runner.wait_until_healthy(host="127.0.0.1", port=port, timeout_seconds=10) is True


def test_wait_until_healthy_false_when_nothing_listening():
    from app.launcher import server_runner
    assert server_runner.wait_until_healthy(host="127.0.0.1", port=_free_port(), timeout_seconds=0.5) is False


def test_request_shutdown_stops_the_background_thread(running_server):
    from app.launcher import server_runner
    running, port = running_server
    assert server_runner.wait_until_healthy(host="127.0.0.1", port=port, timeout_seconds=10) is True

    running.request_shutdown(join_timeout=5)

    assert running.thread.is_alive() is False


def test_request_shutdown_is_safe_to_call_twice(running_server):
    from app.launcher import server_runner
    running, port = running_server
    server_runner.wait_until_healthy(host="127.0.0.1", port=port, timeout_seconds=10)

    running.request_shutdown(join_timeout=5)
    running.request_shutdown(join_timeout=1)  # must not raise


def test_request_shutdown_actually_frees_the_port(running_server):
    """Not just "the Python thread object reports not alive" (the
    existing thread.is_alive() check above) but the real, observable
    property app/launcher/tray.py::do_restart() depends on: a fresh
    listener can bind the exact same port immediately afterward. If
    uvicorn's socket were left lingering, a restart on the same port
    would fail with "address already in use" instead of the health-wait
    simply timing out — silently worse, not louder."""
    from app.launcher import server_runner
    running, port = running_server
    assert server_runner.wait_until_healthy(host="127.0.0.1", port=port, timeout_seconds=10) is True

    running.request_shutdown(join_timeout=5)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))  # raises OSError if still bound by the old server


def test_run_server_logs_and_swallows_a_crashing_server(caplog):
    """Regression guard for a real failure caught on windows-latest CI: a
    real frozen JARVIS.exe launched and stayed running but never
    answered /health, with zero diagnostic trace anywhere — consistent
    with the background uvicorn thread's target (previously bare
    server.run(), passed straight to threading.Thread) raising an
    exception that a console=False build has nowhere to print. This
    proves _run_server() catches and logs instead of losing it, and
    critically does NOT re-raise (a daemon thread's uncaught exception
    doesn't crash the process either way, but re-raising here would
    still lose the traceback the same way)."""
    from app.launcher import server_runner

    class _CrashingServer:
        def run(self):
            raise RuntimeError("simulated frozen-build import failure")

    with caplog.at_level("ERROR"):
        server_runner._run_server(_CrashingServer())  # must not raise

    assert any("crashed" in record.message.lower() for record in caplog.records)
    assert any(record.exc_info for record in caplog.records), "exception traceback must be logged, not just a bare message"


def test_server_starts_successfully_when_stdout_and_stderr_are_none():
    """Regression test for the real root cause behind the CI failure the
    two tests above were added to help diagnose: uvicorn.Config()'s
    default logging setup calls logging.config.dictConfig() on a config
    that wires "ext://sys.stdout"/"ext://sys.stderr" StreamHandlers —
    both are None in a real --windowed/console=False PyInstaller build
    (the exact condition app/logging_config.py's own console-handler fix
    already accounts for), and dictConfig() raises ValueError trying to
    configure them. That exception was never caught anywhere in
    start_server_in_background(), so it reached PyInstaller's own
    bootloader (disable_windowed_traceback=False in packaging/jarvis.spec),
    which shows a native traceback dialog box with no human on a CI
    runner to dismiss it — matching exactly what real CI runs showed: the
    process stayed alive, jarvis.log never received a single line, and
    wait_until_healthy() just timed out with no explanation anywhere.

    This reproduces the real condition directly (not mocked) with the
    real uvicorn library and proves start_server_in_background() still
    produces a genuinely healthy, responding server — the strongest
    possible evidence the fix (log_config=None when stdout/stderr are
    None) actually works, not just that the right keyword was passed."""
    from app.launcher import server_runner

    port = _free_port()
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = None
    sys.stderr = None
    try:
        running = server_runner.start_server_in_background(host="127.0.0.1", port=port)
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr

    try:
        assert server_runner.wait_until_healthy(host="127.0.0.1", port=port, timeout_seconds=10) is True
    finally:
        running.request_shutdown(join_timeout=5)


def test_server_shutdown_releases_tts_resources(running_server):
    """app/api/server.py's lifespan() calls tts_service.stop() on
    shutdown — proves the launcher's clean-shutdown path (uvicorn's
    should_exit -> lifespan shutdown) actually reaches it, not just that
    the source line exists."""
    from app.launcher import server_runner
    running, port = running_server
    assert server_runner.wait_until_healthy(host="127.0.0.1", port=port, timeout_seconds=10) is True

    with patch("app.voice.tts.tts_service.stop") as mock_stop:
        running.request_shutdown(join_timeout=5)

    mock_stop.assert_called_once()
