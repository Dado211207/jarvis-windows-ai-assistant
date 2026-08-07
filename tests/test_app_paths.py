"""Tests for app/core/app_paths.py — the authoritative dev-vs-packaged path resolver.

Packaged mode is simulated by monkeypatching sys.frozen/sys._MEIPASS (the
same attributes PyInstaller sets on a real frozen process) rather than
requiring an actual frozen build, matching how app/api/server.py and
app/ui/routes.py's existing frozen-detection is exercised in this suite.
"""

import sys
from pathlib import Path

import pytest


def _make_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_meipass"), raising=False)


def _make_unfrozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)


# ---------------------------------------------------------------------------
# is_frozen()
# ---------------------------------------------------------------------------

def test_is_frozen_false_under_pytest(monkeypatch):
    from app.core import app_paths
    _make_unfrozen(monkeypatch)
    assert app_paths.is_frozen() is False


def test_is_frozen_true_when_both_attrs_present(monkeypatch, tmp_path):
    from app.core import app_paths
    _make_frozen(monkeypatch, tmp_path)
    assert app_paths.is_frozen() is True


def test_is_frozen_false_when_only_frozen_flag_set(monkeypatch):
    """sys.frozen alone (no _MEIPASS) must not count as frozen — matches
    the existing detection in app/api/server.py::_ui_static_dir()."""
    from app.core import app_paths
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert app_paths.is_frozen() is False


# ---------------------------------------------------------------------------
# app_data_root() — dev mode
# ---------------------------------------------------------------------------

def test_dev_mode_root_is_repo_root(monkeypatch):
    from app.core import app_paths
    _make_unfrozen(monkeypatch)
    root = app_paths.app_data_root()
    assert (root / "app" / "core" / "app_paths.py").exists()


# ---------------------------------------------------------------------------
# app_data_root() — packaged mode
# ---------------------------------------------------------------------------

def test_packaged_mode_uses_localappdata(monkeypatch, tmp_path):
    from app.core import app_paths
    _make_frozen(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    root = app_paths.app_data_root()
    assert root == tmp_path / "AppData" / "Local" / "JARVIS"


def test_packaged_mode_never_resolves_inside_repo(monkeypatch, tmp_path):
    from app.core import app_paths
    _make_frozen(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    root = app_paths.app_data_root()
    repo_root = Path(__file__).resolve().parent.parent
    assert repo_root not in root.parents and root != repo_root


def test_packaged_mode_falls_back_without_localappdata(monkeypatch, tmp_path):
    """Missing LOCALAPPDATA (non-Windows) must not raise — falls back to
    a deterministic per-user location instead."""
    from app.core import app_paths
    _make_frozen(monkeypatch, tmp_path)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    root = app_paths.app_data_root()
    assert root.name == "JARVIS"


# ---------------------------------------------------------------------------
# Derived paths
# ---------------------------------------------------------------------------

def test_derived_paths_are_under_app_data_root(monkeypatch, tmp_path):
    from app.core import app_paths
    _make_frozen(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    root = app_paths.app_data_root()

    assert app_paths.data_dir() == root / "data"
    assert app_paths.config_dir() == root / "config"
    assert app_paths.logs_dir() == root / "data" / "logs"
    assert app_paths.models_dir() == root / "models"
    assert app_paths.default_db_path() == root / "data" / "jarvis.db"
    assert app_paths.default_log_file() == root / "data" / "logs" / "jarvis.log"
    assert app_paths.default_screenshots_dir() == root / "data" / "screenshots"


def test_derived_paths_stay_relative_in_dev_mode(monkeypatch):
    from app.core import app_paths
    _make_unfrozen(monkeypatch)
    assert app_paths.default_db_path().name == "jarvis.db"
    assert app_paths.default_db_path().parent.name == "data"
