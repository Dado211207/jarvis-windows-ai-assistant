"""Update checking — compares the running version against GitHub Release
metadata. Never installs anything automatically: at most it tells the user
a newer version exists and links to the release page for them to download
and run themselves.

Repository visibility gate
---------------------------
GitHub's Releases API only serves metadata for a repository without
authentication if that repository is public. As of 2026-07-16, an
unauthenticated fetch of https://github.com/dado211207/jarvis-windows-ai-assistant
returned HTTP 404 — GitHub's standard response for a private repository to a
viewer without access (it never distinguishes "private" from "doesn't
exist", to avoid leaking existence). Sending a real GitHub token from every
installed user's machine just to check for updates would mean shipping a
credential to every end user, which this project never does (see
docs/SECURITY.md). So while the repository stays private, update checking
is deliberately disabled — not attempted, not silently failing, disabled —
and the UI says so plainly instead of showing a spinner that can never
resolve. Flip _REPO_IS_PUBLIC to True (and re-verify) once that changes.
"""

import json
import re
import urllib.error
import urllib.request
from typing import Optional

from app import __version__
from app.logging_config import get_logger

logger = get_logger("update_check")

_REPO_OWNER = "dado211207"
_REPO_NAME = "jarvis-windows-ai-assistant"
_REPO_IS_PUBLIC = False  # see module docstring — do not flip without re-verifying

_RELEASES_API_URL = f"https://api.github.com/repos/{_REPO_OWNER}/{_REPO_NAME}/releases/latest"
_RELEASES_PAGE_URL = f"https://github.com/{_REPO_OWNER}/{_REPO_NAME}/releases/latest"

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(.+))?$")


def is_update_checking_supported() -> bool:
    return _REPO_IS_PUBLIC


def _parse_version(v: str):
    m = _VERSION_RE.match(v.strip())
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    return (int(major), int(minor), int(patch), pre)


def is_newer(candidate: str, current: str) -> bool:
    """True if *candidate* is a newer version than *current*. Unparsable
    strings are never treated as newer (fail closed — no false "update
    available")."""
    c = _parse_version(candidate)
    b = _parse_version(current)
    if c is None or b is None:
        return False
    if c[:3] != b[:3]:
        return c[:3] > b[:3]
    # Same numeric core: no-prerelease beats any prerelease (standard semver
    # precedence), otherwise compare the prerelease strings lexically.
    if c[3] is None and b[3] is not None:
        return True
    if c[3] is not None and b[3] is None:
        return False
    return (c[3] or "") > (b[3] or "")


def check_for_updates() -> dict:
    """Never raises. Never makes a network call unless update checking is
    actually supported (see is_update_checking_supported)."""
    if not is_update_checking_supported():
        return {
            "checked": False,
            "reason": "This repository is currently private, so update checking is unavailable.",
            "current_version": __version__,
            "releases_page": _RELEASES_PAGE_URL,
        }

    try:
        req = urllib.request.Request(
            _RELEASES_API_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "JARVIS-update-check"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        logger.warning("Update check failed: %s", exc)
        return {
            "checked": False,
            "reason": "Could not reach GitHub to check for updates.",
            "current_version": __version__,
            "releases_page": _RELEASES_PAGE_URL,
        }

    latest_version: Optional[str] = data.get("tag_name")
    if not latest_version:
        return {
            "checked": False,
            "reason": "GitHub did not return release information.",
            "current_version": __version__,
            "releases_page": _RELEASES_PAGE_URL,
        }

    update_available = is_newer(latest_version, __version__)
    return {
        "checked": True,
        "current_version": __version__,
        "latest_version": latest_version,
        "update_available": update_available,
        "download_url": data.get("html_url", _RELEASES_PAGE_URL),
        "release_notes": data.get("body", ""),
        "releases_page": _RELEASES_PAGE_URL,
    }
