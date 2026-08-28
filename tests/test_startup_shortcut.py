"""Tests for app/launcher/startup_shortcut.py.

The COM call that writes a real .lnk is injected, so the enable/disable/
state logic is provable on Linux CI. The safety property that matters
most here — only ever touching JARVIS's own shortcut — is tested
directly by putting unrelated files in the same folder and asserting
they survive.
"""

from pathlib import Path

import pytest

from app.launcher import startup_shortcut


@pytest.fixture
def fake_env(tmp_path):
    """A throwaway APPDATA so no test can touch a real Startup folder."""
    return {"APPDATA": str(tmp_path / "AppData" / "Roaming")}


def _startup_dir(fake_env) -> Path:
    return startup_shortcut.startup_dir(fake_env)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def test_startup_dir_resolves_under_appdata(fake_env):
    path = startup_shortcut.startup_dir(fake_env)
    assert path.parts[-5:] == ("Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    assert str(path).startswith(fake_env["APPDATA"])


def test_startup_dir_is_none_without_appdata():
    assert startup_shortcut.startup_dir({}) is None


def test_shortcut_path_is_none_without_appdata():
    assert startup_shortcut.shortcut_path({}) is None


def test_shortcut_is_named_for_jarvis(fake_env):
    assert startup_shortcut.shortcut_path(fake_env).name == "JARVIS.lnk"


# ---------------------------------------------------------------------------
# State is read from disk, never cached
# ---------------------------------------------------------------------------

def test_not_enabled_when_no_shortcut_exists(fake_env):
    assert startup_shortcut.is_enabled(fake_env) is False


def test_enabled_once_the_shortcut_really_exists(fake_env):
    path = startup_shortcut.shortcut_path(fake_env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")

    assert startup_shortcut.is_enabled(fake_env) is True


def test_is_enabled_is_false_without_appdata():
    assert startup_shortcut.is_enabled({}) is False


# ---------------------------------------------------------------------------
# enable()
# ---------------------------------------------------------------------------

def test_enable_writes_a_shortcut_through_the_injected_writer(fake_env):
    written = {}

    def _writer(target, executable, icon):
        written["target"] = target
        written["executable"] = executable
        Path(target).write_text("lnk", encoding="utf-8")

    assert startup_shortcut.enable(fake_env, writer=_writer) is True
    assert startup_shortcut.is_enabled(fake_env) is True
    assert Path(written["target"]).name == "JARVIS.lnk"


def test_enable_creates_the_startup_folder_if_missing(fake_env):
    assert not _startup_dir(fake_env).exists()

    startup_shortcut.enable(fake_env, writer=lambda t, e, i: Path(t).write_text("x", encoding="utf-8"))

    assert _startup_dir(fake_env).exists()


def test_enable_returns_false_without_a_resolvable_startup_folder():
    assert startup_shortcut.enable({}, writer=lambda t, e, i: None) is False


def test_enable_reports_failure_instead_of_raising(fake_env):
    """A settings toggle must be able to say "that did not work" rather
    than crash or silently claim success."""
    def _boom(target, executable, icon):
        raise OSError("access denied")

    assert startup_shortcut.enable(fake_env, writer=_boom) is False
    assert startup_shortcut.is_enabled(fake_env) is False


def test_enable_is_skipped_outside_a_packaged_build(fake_env, monkeypatch):
    """A dev checkout has no single executable to point at; writing a
    shortcut to the bare interpreter would produce something that does
    not start JARVIS."""
    monkeypatch.setattr(startup_shortcut.sys, "platform", "win32", raising=False)
    monkeypatch.setattr(startup_shortcut.sys, "frozen", False, raising=False)

    assert startup_shortcut.enable(fake_env) is False


# ---------------------------------------------------------------------------
# disable() — only ever JARVIS's own shortcut
# ---------------------------------------------------------------------------

def test_disable_removes_the_shortcut(fake_env):
    startup_shortcut.enable(fake_env, writer=lambda t, e, i: Path(t).write_text("x", encoding="utf-8"))
    assert startup_shortcut.is_enabled(fake_env) is True

    assert startup_shortcut.disable(fake_env) is True
    assert startup_shortcut.is_enabled(fake_env) is False


def test_disable_is_idempotent(fake_env):
    """Already-absent is the requested end state, so it is a success."""
    assert startup_shortcut.disable(fake_env) is True
    assert startup_shortcut.disable(fake_env) is True


def test_disable_never_touches_other_startup_entries(fake_env):
    """The safety property: other applications' startup shortcuts must
    survive untouched."""
    directory = _startup_dir(fake_env)
    directory.mkdir(parents=True, exist_ok=True)
    others = [directory / "SomeOtherApp.lnk", directory / "Backup Tool.lnk", directory / "notes.txt"]
    for other in others:
        other.write_text("not ours", encoding="utf-8")
    startup_shortcut.enable(fake_env, writer=lambda t, e, i: Path(t).write_text("x", encoding="utf-8"))

    startup_shortcut.disable(fake_env)

    for other in others:
        assert other.exists(), f"{other.name} must never be removed by JARVIS"


def test_disable_without_appdata_reports_false():
    assert startup_shortcut.disable({}) is False
