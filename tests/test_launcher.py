"""Tests for the PyInstaller entry point (run_jarvis) and run_api helper."""
from unittest.mock import patch


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
