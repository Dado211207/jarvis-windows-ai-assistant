"""Tests for app/launcher/boot_trace.py.

Added after a real CI failure: a frozen JARVIS.exe launched, stayed
running, and left its own jarvis.log (app/logging_config.py's own
RotatingFileHandler) completely empty — meaning whatever went wrong
happened before any logger.*() call ever ran, or logging setup itself
silently produced nothing. boot_trace.trace() is plain file I/O with no
dependency on the logging subsystem, specifically so a startup problem
stays diagnosable even when logging itself is what's failing to explain
it.
"""

from unittest.mock import patch


def test_trace_is_a_noop_when_not_frozen(tmp_path):
    """Dev/test runs must never write anything — app_data_root()
    resolves to the repository root in dev mode, and writing there on
    every test run would leak a boot_trace.log file into the working
    tree, the same class of problem already fixed once for
    jarvis.lock (see .gitignore)."""
    from app.launcher import boot_trace

    with patch("app.core.app_paths.is_frozen", return_value=False), \
         patch("app.core.app_paths.app_data_root", return_value=tmp_path) as mock_root:
        boot_trace.trace("should not be written")

    mock_root.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_trace_writes_the_message_when_frozen(tmp_path):
    from app.launcher import boot_trace

    with patch("app.core.app_paths.is_frozen", return_value=True), \
         patch("app.core.app_paths.app_data_root", return_value=tmp_path):
        boot_trace.trace("hello from a real frozen build")

    trace_file = tmp_path / "boot_trace.log"
    assert trace_file.is_file()
    assert "hello from a real frozen build" in trace_file.read_text(encoding="utf-8")


def test_trace_appends_across_multiple_calls(tmp_path):
    from app.launcher import boot_trace

    with patch("app.core.app_paths.is_frozen", return_value=True), \
         patch("app.core.app_paths.app_data_root", return_value=tmp_path):
        boot_trace.trace("first")
        boot_trace.trace("second")

    content = (tmp_path / "boot_trace.log").read_text(encoding="utf-8")
    assert "first" in content
    assert "second" in content
    assert content.index("first") < content.index("second")


def test_trace_never_raises_even_if_app_data_root_is_broken():
    from app.launcher import boot_trace

    with patch("app.core.app_paths.is_frozen", return_value=True), \
         patch("app.core.app_paths.app_data_root", side_effect=RuntimeError("simulated failure")):
        boot_trace.trace("must not raise")  # would fail the test if it did
