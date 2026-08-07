"""Tests for the PyInstaller entry point (run_jarvis) and run_api helper."""
from unittest.mock import patch


def test_run_api_function_exists():
    """app.api.server must expose run_api() callable for the --api launch mode."""
    from app.api import server
    assert callable(getattr(server, "run_api", None)), (
        "app.api.server.run_api must be a callable"
    )


def test_run_jarvis_default_mode_is_windowed():
    """run_jarvis._run() with no flags delegates to the windowed launcher
    — the only mode that works in a --windowed (console=False) build,
    which is what the installed JARVIS.exe actually is. No longer the
    CLI: see run_jarvis.py's own docstring for why the default changed."""
    import run_jarvis
    with patch("sys.argv", ["JARVIS.exe"]):
        with patch("app.launcher.gui.run_windowed") as mock_run_windowed:
            run_jarvis._run()
    mock_run_windowed.assert_called_once()


def test_run_jarvis_cli_mode():
    """run_jarvis._run() with --cli delegates to app.main.main — preserved
    for console-mode builds/dev use, just no longer the default."""
    import run_jarvis
    with patch("sys.argv", ["JARVIS.exe", "--cli"]):
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
