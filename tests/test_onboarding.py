"""Tests for app/core/onboarding.py's first-run marker file."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path, monkeypatch):
    from app.core import onboarding
    monkeypatch.setattr(onboarding, "config_dir", lambda: tmp_path)
    return tmp_path


def test_marker_path_under_config_dir(tmp_path):
    from app.core import onboarding
    assert onboarding.marker_path() == tmp_path / "onboarding_complete"


def test_not_complete_when_marker_absent():
    from app.core import onboarding
    assert onboarding.is_onboarding_complete() is False


def test_complete_after_marking(tmp_path):
    from app.core import onboarding
    onboarding.mark_onboarding_complete()
    assert onboarding.is_onboarding_complete() is True
    assert (tmp_path / "onboarding_complete").exists()


def test_marking_complete_creates_parent_directories(tmp_path, monkeypatch):
    from app.core import onboarding
    nested = tmp_path / "does" / "not" / "exist" / "yet"
    monkeypatch.setattr(onboarding, "config_dir", lambda: nested)

    onboarding.mark_onboarding_complete()

    assert (nested / "onboarding_complete").exists()


def test_marking_complete_twice_is_safe():
    from app.core import onboarding
    onboarding.mark_onboarding_complete()
    onboarding.mark_onboarding_complete()  # must not raise
    assert onboarding.is_onboarding_complete() is True
