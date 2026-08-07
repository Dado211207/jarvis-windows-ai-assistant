"""Tests for app/launcher/server_runner.py — real uvicorn on a real
ephemeral port, the same "actually bind and poll /health" approach
scripts/ci_windows_smoke.py and tests/test_playwright_e2e.py's server
fixture already use, not a mocked HTTP layer.
"""

import socket

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
