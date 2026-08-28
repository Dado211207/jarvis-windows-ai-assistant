"""Tracks whether first-run onboarding has been completed — a small
marker file under app_paths.config_dir(), not a database table (this is
UI-flow state, not user data). A missing file means "not yet seen,"
which is exactly right for a fresh %LOCALAPPDATA%\\JARVIS\\config\\
directory on a genuine first run — no separate "is this the first
launch" flag to keep in sync.
"""

from pathlib import Path

from app.core.app_paths import config_dir

MARKER_FILENAME = "onboarding_complete"


def marker_path() -> Path:
    return config_dir() / MARKER_FILENAME


def is_onboarding_complete() -> bool:
    return marker_path().exists()


def mark_onboarding_complete() -> None:
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
