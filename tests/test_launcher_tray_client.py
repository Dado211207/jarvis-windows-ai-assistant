"""Tests for app/launcher/tray_client.py against a real running server —
same ephemeral-port-plus-server_runner approach as
tests/test_launcher_server_runner.py, so the cookie/header double-submit
dance is proven against the real app.api.session middleware, not a
mocked httpx transport.
"""

import socket

import pytest

from app.core.privacy import privacy_mode


@pytest.fixture(autouse=True)
def reset_privacy_mode():
    # privacy_mode is a process-wide singleton (see tests/test_privacy.py) —
    # this file toggles it through a real server, so it must clean up
    # exactly like that file does.
    privacy_mode.set(False)
    yield
    privacy_mode.set(False)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_client():
    from app.launcher import server_runner
    from app.launcher.tray_client import TrayApiClient

    port = _free_port()
    running = server_runner.start_server_in_background(host="127.0.0.1", port=port)
    assert server_runner.wait_until_healthy(host="127.0.0.1", port=port, timeout_seconds=10)

    client = TrayApiClient("127.0.0.1", port)
    yield client
    client.close()
    running.request_shutdown()


def test_is_healthy_true_against_real_server(live_client):
    assert live_client.is_healthy() is True


def test_is_healthy_false_when_unreachable():
    from app.launcher.tray_client import TrayApiClient
    client = TrayApiClient("127.0.0.1", _free_port())
    try:
        assert client.is_healthy() is False
    finally:
        client.close()


def test_privacy_active_reflects_real_default_off_state(live_client):
    assert live_client.privacy_active() is False


def test_privacy_active_none_when_unreachable():
    from app.launcher.tray_client import TrayApiClient
    client = TrayApiClient("127.0.0.1", _free_port())
    try:
        assert client.privacy_active() is None
    finally:
        client.close()


def test_set_privacy_mode_round_trip_against_real_server(live_client):
    assert live_client.set_privacy_mode(True) is True
    assert live_client.privacy_active() is True

    assert live_client.set_privacy_mode(False) is True
    assert live_client.privacy_active() is False


def test_set_privacy_mode_false_when_unreachable():
    from app.launcher.tray_client import TrayApiClient
    client = TrayApiClient("127.0.0.1", _free_port())
    try:
        assert client.set_privacy_mode(True) is False
    finally:
        client.close()
