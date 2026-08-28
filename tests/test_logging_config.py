"""Tests for app/logging_config.py.

The console-handler guard here is a real fix, not speculative
hardening: sys.stdout is None in a --windowed/console=False PyInstaller
build (no console to attach to), and setup_logging() used to construct
logging.StreamHandler(sys.stdout) unconditionally — harmless (logging's
own Handler.handleError() swallows the resulting emit() failure) but
pointless, and worth skipping outright rather than silently manufacturing
dead handlers.
"""

import logging

import pytest


@pytest.fixture
def isolated_log_file(tmp_path, monkeypatch):
    """Points settings.log_file at a throwaway path so this file's tests
    never depend on or interfere with the shared dev-mode data/logs/
    directory other tests may also be writing to. Also pins
    jarvis_log_level to DEBUG regardless of the ambient environment —
    .github/workflows/ci.yml's own jobs set JARVIS_LOG_LEVEL=WARNING,
    which silently filtered out this file's own logger.info() call
    before it ever reached a handler and failed this test for real on
    both the ubuntu-latest and windows-latest CI jobs, while passing
    locally where that env var happened to be unset. A test must not
    depend on an ambient setting it never controls."""
    from app.config import settings
    log_path = tmp_path / "test.log"
    monkeypatch.setattr(settings, "jarvis_log_file", str(log_path))
    monkeypatch.setattr(settings, "jarvis_log_level", "DEBUG")
    return log_path


def _handler_types(root: logging.Logger) -> list:
    return [type(h).__name__ for h in root.handlers]


def test_file_handler_always_added(isolated_log_file, monkeypatch):
    monkeypatch.setattr("sys.stdout", object())  # any non-None value
    from app.logging_config import setup_logging
    root = setup_logging()
    assert "RotatingFileHandler" in _handler_types(root)


def test_console_handler_added_when_stdout_present(isolated_log_file, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdout", io.StringIO())
    from app.logging_config import setup_logging
    root = setup_logging()
    assert "StreamHandler" in _handler_types(root)


def test_console_handler_skipped_when_stdout_is_none(isolated_log_file, monkeypatch):
    """The actual --windowed PyInstaller condition this guard exists for."""
    monkeypatch.setattr("sys.stdout", None)
    from app.logging_config import setup_logging
    root = setup_logging()
    assert "StreamHandler" not in _handler_types(root)
    assert "RotatingFileHandler" in _handler_types(root)


def test_logging_still_writes_to_file_when_stdout_is_none(isolated_log_file, monkeypatch):
    """Not just "a handler is attached" — a real emitted record actually
    lands in the file, proving the file handler works independently of
    whatever state sys.stdout is in."""
    monkeypatch.setattr("sys.stdout", None)
    from app.logging_config import get_logger, setup_logging
    root = setup_logging()
    logger = get_logger("test_logging_config")
    logger.info("hello with no console")

    for handler in root.handlers:
        handler.flush()
    content = isolated_log_file.read_text(encoding="utf-8")
    assert "hello with no console" in content
