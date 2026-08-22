"""A one-bit "show yourself" signal from a second launch to the running
instance.

Clicking the Start-menu shortcut while JARVIS is already running is one
of the ordinary ways people open an app they think is closed. The old
answer was to open the dashboard in a browser, which is how the browser
came to look like the product's real interface. The right answer is to
focus the window that already exists — but the second process has no
handle on the first one's window, and no channel into a parent it does
not own.

So: a marker file the running instance polls. Deliberately the least
powerful mechanism that solves the problem.

  - It carries **no data**. Its existence is the entire message, and the
    only thing it can ever cause is "show the window". There is nothing
    to parse, so there is nothing to parse wrongly.
  - It is not a control channel. The authenticated IPC in
    app/launcher/ipc.py stays the only way to command the window; this
    just tells the parent to use it.
  - It lives beside the instance lock, under the JARVIS data directory,
    so it inherits the same per-user location and is removed with the
    rest of the app's data on an opt-in uninstall.

A stale marker left by a crash costs one unnecessary window focus on the
next start, which is why it is cleared on startup rather than trusted.
"""

import time
from pathlib import Path
from typing import Optional

from app.logging_config import get_logger

logger = get_logger("launcher.attention")

MARKER_FILENAME = "show-window.request"
# Older than this and the marker is a leftover, not a request: a user
# waiting on a window would have given up and clicked again long before.
MAX_AGE_SECONDS = 30.0


def marker_path() -> Path:
    from app.core.app_paths import app_data_root
    return app_data_root() / MARKER_FILENAME


def request() -> bool:
    """Ask the running instance to show its window. Never raises — a
    second launch that cannot write the marker should still exit
    quietly rather than crash in front of the user."""
    try:
        path = marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()), encoding="utf-8")
        return True
    except OSError:
        logger.warning("Could not signal the running instance.", exc_info=True)
        return False


def consume() -> bool:
    """True exactly once per request, then clears it.

    Ignores and clears a marker older than MAX_AGE_SECONDS so a crash
    cannot make the window pop up unbidden minutes later.
    """
    path = marker_path()
    try:
        if not path.exists():
            return False
        age = time.time() - path.stat().st_mtime
        path.unlink(missing_ok=True)
        return age <= MAX_AGE_SECONDS
    except OSError:
        return False


def clear() -> None:
    """Drop any marker left behind by a previous run. Called at startup
    so a stale request is never mistaken for a live one."""
    try:
        marker_path().unlink(missing_ok=True)
    except OSError:
        pass
