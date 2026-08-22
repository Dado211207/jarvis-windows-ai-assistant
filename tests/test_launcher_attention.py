"""Tests for app/launcher/attention.py — the "show yourself" signal a
second launch leaves for the instance that is already running.

Everything here runs against a real temporary data directory: the module
is a handful of filesystem operations, and mocking them would leave the
one behaviour that matters — a stale marker must not resurface — proven
against nothing.
"""

import time

import pytest

from app.launcher import attention


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.app_paths.app_data_root", lambda: tmp_path)
    return tmp_path


def test_no_request_means_nothing_to_consume(data_root):
    assert attention.consume() is False


def test_a_request_is_consumed_exactly_once(data_root):
    assert attention.request() is True

    assert attention.consume() is True
    assert attention.consume() is False, "one click must not show the window twice"


def test_a_stale_marker_is_ignored_and_cleared(data_root):
    """A marker left by a crash must not make the window pop up unbidden
    minutes later — by then the user has long since given up and clicked
    again."""
    attention.request()
    marker = attention.marker_path()
    old = time.time() - (attention.MAX_AGE_SECONDS + 60)
    import os
    os.utime(marker, (old, old))

    assert attention.consume() is False
    assert not marker.exists(), "a stale marker must be cleared, not left to be re-read"


def test_clear_removes_a_pending_request(data_root):
    attention.request()
    attention.clear()
    assert attention.consume() is False


def test_clear_on_a_clean_directory_is_silent(data_root):
    attention.clear()  # must not raise


def test_the_marker_carries_no_instructions(data_root):
    """Its existence is the entire message. Nothing is parsed out of it,
    so nothing in it can be parsed wrongly — this is deliberately the
    least powerful mechanism that solves the problem, not a second
    control channel alongside the authenticated IPC."""
    attention.request()
    contents = attention.marker_path().read_text(encoding="utf-8")

    float(contents)  # a bare timestamp, and nothing else
    assert "\n" not in contents


def test_a_request_that_cannot_be_written_fails_quietly(monkeypatch, data_root):
    """A second launch that cannot signal the first should still exit
    quietly rather than crash in front of the user."""
    def _explode(*args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("pathlib.Path.write_text", _explode)

    assert attention.request() is False


def test_consume_survives_an_unreadable_marker(monkeypatch, data_root):
    attention.request()

    def _explode(*args, **kwargs):
        raise OSError("gone mid-read")

    monkeypatch.setattr("pathlib.Path.stat", _explode)

    assert attention.consume() is False  # must not raise


def test_the_marker_lives_with_the_rest_of_the_apps_data(data_root):
    """So it inherits the same per-user location and is removed with
    everything else on an opt-in uninstall."""
    attention.request()
    assert attention.marker_path().parent == data_root
