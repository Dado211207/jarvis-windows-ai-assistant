"""Tests for the PyInstaller entry point (run_jarvis) and run_api helper."""
from unittest.mock import patch

import pytest


def test_run_api_function_exists():
    """app.api.server must expose run_api() callable for the --api launch mode."""
    from app.api import server
    assert callable(getattr(server, "run_api", None)), (
        "app.api.server.run_api must be a callable"
    )


def test_run_jarvis_cli_mode():
    """run_jarvis._run() with no --api flag delegates to app.main.main."""
    import run_jarvis
    with patch("sys.argv", ["JARVIS.exe"]):
        with patch("app.main.main") as mock_main:
            run_jarvis._run()
    mock_main.assert_called_once()


def test_run_jarvis_api_mode():
    """run_jarvis._run() with --api flag delegates to app.api.server.run_api."""
    import run_jarvis
    with patch("sys.argv", ["JARVIS.exe", "--api"]):
        with patch("app.api.server.run_api") as mock_run_api:
            run_jarvis._run()
    mock_run_api.assert_called_once()


def test_run_jarvis_explicit_cli_flag():
    """--cli forces the terminal CLI even if a frozen build were detected."""
    import run_jarvis
    with patch("sys.argv", ["JARVIS.exe", "--cli"]):
        with patch("run_jarvis._is_frozen", return_value=True):
            with patch("app.main.main") as mock_main:
                run_jarvis._run()
    mock_main.assert_called_once()


def test_run_jarvis_dev_mode_ignores_launcher():
    """Not frozen + no flags still goes to the CLI, never the production launcher."""
    import run_jarvis
    with patch("sys.argv", ["JARVIS.exe"]):
        with patch("run_jarvis._is_frozen", return_value=False):
            with patch("app.main.main") as mock_main:
                with patch("app.core.launcher.run_production") as mock_launcher:
                    run_jarvis._run()
    mock_main.assert_called_once()
    mock_launcher.assert_not_called()


def test_run_jarvis_frozen_mode_uses_production_launcher():
    """Frozen + no flags delegates to the no-console production launcher."""
    import run_jarvis
    with patch("sys.argv", ["JARVIS.exe"]):
        with patch("run_jarvis._is_frozen", return_value=True):
            with patch("app.core.launcher.run_production", return_value=0) as mock_launcher:
                with pytest.raises(SystemExit) as exc_info:
                    run_jarvis._run()
    mock_launcher.assert_called_once()
    assert exc_info.value.code == 0
