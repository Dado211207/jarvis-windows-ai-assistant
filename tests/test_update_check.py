"""Tests for update checking (app/core/update_check.py, its API route, and
the Settings page's "About & Updates" section).

The repository is currently private (verified via an unauthenticated fetch
returning 404 — see the module docstring), so is_update_checking_supported()
must be False and check_for_updates() must never make a real network call.
The "if it were public" code paths are still exercised here with the
GitHub API call fully mocked.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.core import update_check


def test_update_checking_currently_disabled():
    # This is the whole point of the private-repo gate — if this ever flips
    # to True, is_newer()/check_for_updates()'s real HTTP path must be
    # re-verified against the actual (now public) repository first.
    assert update_check.is_update_checking_supported() is False


def test_check_for_updates_never_calls_network_when_unsupported():
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = update_check.check_for_updates()
    mock_urlopen.assert_not_called()
    assert result["checked"] is False
    assert "private" in result["reason"]
    assert result["current_version"] == update_check.__version__


def test_check_for_updates_includes_releases_page_link():
    result = update_check.check_for_updates()
    assert result["releases_page"].startswith("https://github.com/")


# --- is_newer() semver-ish comparison ---

@pytest.mark.parametrize("candidate,current,expected", [
    ("v0.2.0", "0.1.7", True),
    ("0.1.8", "0.1.7", True),
    ("0.1.7", "0.1.7", False),
    ("0.1.6", "0.1.7", False),
    ("0.1.7", "0.1.7-alpha", True),      # no-prerelease beats prerelease
    ("0.1.7-alpha", "0.1.7", False),
    ("0.1.7-beta", "0.1.7-alpha", True),
])
def test_is_newer(candidate, current, expected):
    assert update_check.is_newer(candidate, current) is expected


def test_is_newer_fails_closed_on_unparsable_versions():
    assert update_check.is_newer("not-a-version", "0.1.7") is False
    assert update_check.is_newer("0.1.8", "also-not-a-version") is False


# --- check_for_updates() with checking force-enabled (simulates the repo
#     going public — the real HTTP path, fully mocked) ---

def _fake_response(payload: dict):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    return mock_resp


def test_check_for_updates_reports_update_available_when_supported():
    payload = {"tag_name": "v0.2.0", "html_url": "https://github.com/x/y/releases/tag/v0.2.0", "body": "New stuff"}
    with patch("app.core.update_check.is_update_checking_supported", return_value=True), \
         patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        result = update_check.check_for_updates()
    assert result["checked"] is True
    assert result["update_available"] is True
    assert result["latest_version"] == "v0.2.0"
    assert result["download_url"] == payload["html_url"]
    assert result["release_notes"] == "New stuff"


def test_check_for_updates_reports_up_to_date_when_supported():
    payload = {"tag_name": f"v{update_check.__version__}"}
    with patch("app.core.update_check.is_update_checking_supported", return_value=True), \
         patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        result = update_check.check_for_updates()
    assert result["checked"] is True
    assert result["update_available"] is False


def test_check_for_updates_handles_network_failure_gracefully():
    import urllib.error
    with patch("app.core.update_check.is_update_checking_supported", return_value=True), \
         patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no network")):
        result = update_check.check_for_updates()
    assert result["checked"] is False
    assert "reach" in result["reason"].lower()


def test_check_for_updates_handles_missing_tag_name():
    with patch("app.core.update_check.is_update_checking_supported", return_value=True), \
         patch("urllib.request.urlopen", return_value=_fake_response({})):
        result = update_check.check_for_updates()
    assert result["checked"] is False


# --- API route ---

@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield client


def test_update_check_endpoint_returns_200(api_client):
    r = api_client.get("/update/check")
    assert r.status_code == 200
    body = r.json()
    assert body["checked"] is False
    assert "current_version" in body


def test_update_check_endpoint_no_secrets(api_client):
    r = api_client.get("/update/check")
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-" not in r.text


# --- Settings page markup ---

def test_settings_page_has_update_section(api_client):
    r = api_client.get("/ui/settings")
    html = r.text
    assert "update-check-btn" in html
    assert "update-current-version" in html
    assert "update-download-link" in html
