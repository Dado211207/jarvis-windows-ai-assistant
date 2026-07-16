"""Tests for app/logging_config.py — specifically that setup_logging() never
crashes when sys.stdout is None, which is how a PyInstaller --windowed
(no console) build looks at runtime."""

from unittest.mock import patch

from app.logging_config import setup_logging


def test_setup_logging_works_normally():
    logger = setup_logging()
    assert logger.name == "jarvis"
    assert len(logger.handlers) >= 1


def test_setup_logging_skips_console_handler_when_stdout_is_none():
    import logging
    import logging.handlers as lh
    with patch("sys.stdout", None):
        logger = setup_logging()  # must not raise
    # only the rotating file handler should be present, no StreamHandler on None
    stream_handlers = [
        h for h in logger.handlers
        if type(h) is logging.StreamHandler  # RotatingFileHandler subclasses StreamHandler too
    ]
    assert stream_handlers == []
    assert any(isinstance(h, lh.RotatingFileHandler) for h in logger.handlers)


def test_setup_logging_emits_without_raising_when_stdout_is_none():
    with patch("sys.stdout", None):
        logger = setup_logging()
        logger.info("this must not raise even though stdout is None")
