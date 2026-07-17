"""Tests for the centralized production path service (app/core/paths.py).

Dev-mode behaviour must stay byte-identical to the pre-installer layout;
frozen-mode behaviour is exercised via JARVIS_APPDATA_OVERRIDE so these
tests never touch a real user profile, on any OS.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import paths


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("JARVIS_APPDATA_OVERRIDE", raising=False)
    monkeypatch.delenv("JARVIS_DB_PATH", raising=False)
    monkeypatch.delenv("JARVIS_LOG_FILE", raising=False)
    yield


def test_is_frozen_false_under_pytest():
    assert paths.is_frozen() is False


def test_dev_mode_data_dir_is_repo_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = paths.data_dir()
    assert d == Path("data")
    assert d.exists()


def test_dev_mode_db_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert paths.db_path() == Path("data") / "jarvis.db"


def test_dev_mode_logs_dir_nested_under_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert paths.logs_dir() == Path("data") / "logs"


def test_dev_mode_legacy_candidates_empty():
    assert paths.legacy_db_candidates() == []


def test_dev_mode_seed_production_env_is_noop(monkeypatch):
    monkeypatch.delenv("JARVIS_DB_PATH", raising=False)
    paths.seed_production_env()
    assert "JARVIS_DB_PATH" not in os.environ


@patch("app.core.paths.is_frozen", return_value=True)
def test_frozen_mode_uses_appdata_override(mock_frozen, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_APPDATA_OVERRIDE", str(tmp_path))
    assert paths.app_root() == tmp_path / "JARVIS"
    assert paths.data_dir() == tmp_path / "JARVIS" / "data"
    assert paths.logs_dir() == tmp_path / "JARVIS" / "logs"
    assert paths.cache_dir() == tmp_path / "JARVIS" / "cache"
    assert paths.backups_dir() == tmp_path / "JARVIS" / "backups"
    assert paths.config_dir() == tmp_path / "JARVIS" / "config"
    assert paths.db_path() == tmp_path / "JARVIS" / "data" / "jarvis.db"
    # logs/cache/backups/config are siblings of data, not nested under it.
    assert paths.logs_dir().parent == paths.data_dir().parent


@patch("app.core.paths.is_frozen", return_value=True)
def test_frozen_mode_directories_are_created(mock_frozen, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_APPDATA_OVERRIDE", str(tmp_path))
    for fn in (paths.data_dir, paths.logs_dir, paths.cache_dir, paths.backups_dir, paths.config_dir):
        d = fn()
        assert d.exists() and d.is_dir()


@patch("app.core.paths.is_frozen", return_value=True)
def test_seed_production_env_sets_when_absent(mock_frozen, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_APPDATA_OVERRIDE", str(tmp_path))
    monkeypatch.delenv("JARVIS_DB_PATH", raising=False)
    monkeypatch.delenv("JARVIS_LOG_FILE", raising=False)
    paths.seed_production_env()
    assert os.environ["JARVIS_DB_PATH"] == str(paths.db_path())
    assert os.environ["JARVIS_LOG_FILE"] == str(paths.log_file_path())


@patch("app.core.paths.is_frozen", return_value=True)
def test_seed_production_env_never_overwrites_explicit_value(mock_frozen, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_APPDATA_OVERRIDE", str(tmp_path))
    monkeypatch.setenv("JARVIS_DB_PATH", "/explicit/override.db")
    paths.seed_production_env()
    assert os.environ["JARVIS_DB_PATH"] == "/explicit/override.db"


@patch("app.core.paths.is_frozen", return_value=True)
def test_frozen_legacy_candidates_points_beside_executable(mock_frozen, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_APPDATA_OVERRIDE", str(tmp_path))
    with patch("app.core.paths.installed_program_dir", return_value=tmp_path / "Programs" / "JARVIS"):
        candidates = paths.legacy_db_candidates()
    assert candidates == [tmp_path / "Programs" / "JARVIS" / "data" / "jarvis.db"]


def test_installed_program_dir_dev_mode_is_repo_root():
    d = paths.installed_program_dir()
    assert (d / "app").is_dir()
    assert (d / "requirements.txt").is_file()
