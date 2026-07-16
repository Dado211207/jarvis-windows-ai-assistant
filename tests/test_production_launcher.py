"""Tests for the production launcher's own orchestration logic
(app/core/launcher.py) — port selection, single-instance locking, health
polling, and startup orchestration.

For run_jarvis.py's entry-point delegation (which flag picks which mode),
see tests/test_launcher.py.

Only exercised for frozen builds in real life, so every test isolates paths
the same way tests/test_paths.py and tests/test_secret_store.py do
(JARVIS_APPDATA_OVERRIDE + a tmp_path chdir) — nothing here ever touches the
real repo's data/ directory or a real user profile. Networking, browser
opening, and dialogs are all mocked; only find_free_port/wait_for_health use
real (loopback-only) sockets since that's cheap and deterministic.
"""

import json
import os
import socket
from unittest.mock import MagicMock, patch

import pytest

from app.core import launcher


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JARVIS_APPDATA_OVERRIDE", str(tmp_path))
    with patch("app.core.launcher.paths.is_frozen", return_value=True):
        yield


# --- find_free_port ---

def test_find_free_port_returns_preferred_when_available():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    assert launcher.find_free_port(free_port) == free_port


def test_find_free_port_falls_back_when_preferred_taken():
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken_port = blocker.getsockname()[1]
        result = launcher.find_free_port(taken_port)
        assert result != taken_port
        assert result > 0
    finally:
        blocker.close()


# --- is_process_alive ---

def test_is_process_alive_true_for_current_process():
    assert launcher.is_process_alive(os.getpid()) is True


def test_is_process_alive_false_for_invalid_pid():
    assert launcher.is_process_alive(0) is False
    assert launcher.is_process_alive(-1) is False


def test_is_process_alive_false_when_lookup_fails():
    with patch("app.core.launcher.os.kill", side_effect=ProcessLookupError()):
        assert launcher.is_process_alive(999999) is False


# --- single-instance lock ---

def test_try_acquire_lock_when_none_exists():
    result = launcher.try_acquire_lock(5555)
    assert result is None
    stored = json.loads(launcher._lock_path().read_text())
    assert stored["pid"] == os.getpid()
    assert stored["port"] == 5555


def test_try_acquire_lock_overwrites_stale_lock():
    launcher._lock_path().parent.mkdir(parents=True, exist_ok=True)
    launcher._lock_path().write_text(json.dumps({"pid": 999999999, "port": 1234}))
    with patch("app.core.launcher.is_process_alive", return_value=False):
        result = launcher.try_acquire_lock(6000)
    assert result is None
    stored = json.loads(launcher._lock_path().read_text())
    assert stored["port"] == 6000


def test_try_acquire_lock_returns_existing_when_alive():
    launcher._lock_path().parent.mkdir(parents=True, exist_ok=True)
    launcher._lock_path().write_text(json.dumps({"pid": 4242, "port": 7000}))
    with patch("app.core.launcher.is_process_alive", return_value=True):
        result = launcher.try_acquire_lock(8000)
    assert result == {"pid": 4242, "port": 7000}
    # must not have been overwritten with our own attempted port
    stored = json.loads(launcher._lock_path().read_text())
    assert stored["port"] == 7000


def test_release_lock_removes_own_lock():
    launcher.try_acquire_lock(5555)
    assert launcher._lock_path().exists()
    launcher.release_lock()
    assert not launcher._lock_path().exists()


def test_release_lock_leaves_other_process_lock_alone():
    launcher._lock_path().parent.mkdir(parents=True, exist_ok=True)
    launcher._lock_path().write_text(json.dumps({"pid": 4242, "port": 7000}))
    launcher.release_lock()
    assert launcher._lock_path().exists()


# --- wait_for_health ---

def test_wait_for_health_true_when_endpoint_ok():
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=mock_resp):
        assert launcher.wait_for_health(5555, timeout=1.0, interval=0.05) is True


def test_wait_for_health_false_on_timeout():
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        assert launcher.wait_for_health(5555, timeout=0.2, interval=0.05) is False


# --- open_browser / show_error_dialog ---

def test_open_browser_opens_expected_url():
    with patch("webbrowser.open") as mock_open:
        launcher.open_browser(5555)
    mock_open.assert_called_once_with("http://127.0.0.1:5555/ui/")


def test_show_error_dialog_never_raises():
    launcher.show_error_dialog("Title", "Something went wrong.")  # must not raise


def test_show_error_dialog_swallows_platform_errors():
    with patch("app.core.launcher.sys.platform", "win32"), \
         patch("builtins.__import__", side_effect=ImportError("no ctypes here")):
        launcher.show_error_dialog("Title", "Message")  # must not raise


# --- run_production orchestration (fully mocked collaborators) ---

def test_run_production_bring_existing_to_foreground():
    with patch("app.core.launcher.find_free_port", return_value=5555), \
         patch("app.core.launcher.try_acquire_lock", return_value={"pid": 111, "port": 4444}), \
         patch("app.core.launcher.open_browser") as mock_open, \
         patch("app.core.launcher._build_server") as mock_build:
        code = launcher.run_production()
    assert code == 0
    mock_open.assert_called_once_with(4444)
    mock_build.assert_not_called()


def test_run_production_happy_path():
    mock_server = MagicMock()
    mock_thread = MagicMock()
    with patch("app.core.launcher.find_free_port", return_value=5555), \
         patch("app.core.launcher.try_acquire_lock", return_value=None), \
         patch("app.core.launcher._build_server", return_value=mock_server), \
         patch("app.core.launcher._serve_in_thread", return_value=mock_thread) as mock_serve, \
         patch("app.core.launcher._install_signal_handlers") as mock_signals, \
         patch("app.core.launcher.wait_for_health", return_value=True), \
         patch("app.core.launcher.open_browser") as mock_open, \
         patch("app.core.launcher.release_lock") as mock_release:
        code = launcher.run_production()
    assert code == 0
    mock_serve.assert_called_once_with(mock_server)
    mock_signals.assert_called_once_with(mock_server)
    mock_open.assert_called_once_with(5555)
    mock_thread.join.assert_called_once_with()
    mock_release.assert_called_once()


def test_run_production_health_timeout_shows_dialog_and_stops_server():
    mock_server = MagicMock()
    mock_thread = MagicMock()
    with patch("app.core.launcher.find_free_port", return_value=5555), \
         patch("app.core.launcher.try_acquire_lock", return_value=None), \
         patch("app.core.launcher._build_server", return_value=mock_server), \
         patch("app.core.launcher._serve_in_thread", return_value=mock_thread), \
         patch("app.core.launcher._install_signal_handlers"), \
         patch("app.core.launcher.wait_for_health", return_value=False), \
         patch("app.core.launcher.show_error_dialog") as mock_dialog, \
         patch("app.core.launcher.release_lock") as mock_release:
        code = launcher.run_production()
    assert code == 1
    assert mock_server.should_exit is True
    mock_thread.join.assert_called_once_with(timeout=5.0)
    mock_dialog.assert_called_once()
    mock_release.assert_called_once()


def test_run_production_port_failure_shows_dialog():
    # No port was ever found, so no lock was ever written — release_lock()
    # correctly is not invoked in this branch (nothing of ours to release).
    with patch("app.core.launcher.find_free_port", side_effect=RuntimeError("no ports")), \
         patch("app.core.launcher.show_error_dialog") as mock_dialog, \
         patch("app.core.launcher.release_lock") as mock_release:
        code = launcher.run_production()
    assert code == 1
    mock_dialog.assert_called_once()
    mock_release.assert_not_called()


def test_run_production_unexpected_error_shows_dialog_and_releases_lock():
    with patch("app.core.launcher.find_free_port", return_value=5555), \
         patch("app.core.launcher.try_acquire_lock", return_value=None), \
         patch("app.core.launcher._build_server", side_effect=RuntimeError("boom")), \
         patch("app.core.launcher.show_error_dialog") as mock_dialog, \
         patch("app.core.launcher.release_lock") as mock_release:
        code = launcher.run_production()
    assert code == 1
    mock_dialog.assert_called_once()
    mock_release.assert_called_once()
