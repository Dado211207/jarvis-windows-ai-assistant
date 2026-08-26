"""Tests for the Settings and About/Diagnostics pages and the
/diagnostics endpoint.

The API tests here matter most: the diagnostics endpoint is what the
Copy-report button puts on a user's clipboard, so "no credential ever
appears in the response" is tested against the live endpoint with real
secret-shaped values planted, not only at the module level.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import app

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "app" / "ui" / "templates"
APP_JS = REPO_ROOT / "app" / "ui" / "static" / "app.js"


@pytest.fixture
def client():
    """Primed with the session token, exactly as the dashboard's own JS
    primes itself by reading the (deliberately non-HttpOnly) cookie.

    /diagnostics is a *protected* read: its Locations section names
    %LOCALAPPDATA% paths, and on Windows a full user path contains the
    account name. See test_diagnostics_requires_a_session_token.
    """
    from tests.conftest import prime_session

    with TestClient(app) as test_client:
        yield prime_session(test_client)


# ---------------------------------------------------------------------------
# Pages render and are reachable from the sidebar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/ui/settings", "/ui/diagnostics"])
def test_page_renders(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@pytest.mark.parametrize("href", ["/ui/settings", "/ui/diagnostics"])
def test_page_is_linked_from_the_sidebar(href):
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert f'href="{href}"' in base


def test_sidebar_marks_the_active_page(client):
    """Every nav entry uses aria-current so assistive tech announces
    which page is open."""
    body = client.get("/ui/settings").text
    assert 'aria-current="page"' in body


# ---------------------------------------------------------------------------
# /diagnostics endpoint
# ---------------------------------------------------------------------------

def test_diagnostics_endpoint_returns_sections_and_text(client):
    body = client.get("/diagnostics").json()
    assert body["sections"]
    assert isinstance(body["text"], str)
    assert "JARVIS diagnostic report" in body["text"]


def test_diagnostics_requires_a_session_token():
    """This replaces an earlier assertion that /diagnostics was readable
    *without* a token, on the reasoning that a read-only endpoint should
    not need the mutation token.

    That reasoning no longer holds, for a specific reason: the report's
    Locations section names the application data, logs, configuration and
    model directories, and on Windows a full user path contains the
    account name. It is a personal read, not a public one — so it is
    protected like /memory, /conversation and /logs, and the token is no
    longer only a mutation token.

    Nothing is lost for a real user: the dashboard sends the header on
    protected reads, and when the session mechanism itself is broken the
    log file on disk is the break-glass path, not this endpoint.
    """
    with TestClient(app) as unprimed:
        assert unprimed.get("/diagnostics").status_code == 403


def test_diagnostics_response_never_contains_an_api_key(client, monkeypatch):
    """The whole point of the page: this response goes onto a user's
    clipboard and into bug reports."""
    from app.config import settings
    monkeypatch.setattr(type(settings), "effective_api_key", property(lambda self: "sk-clipboard-leak-test"))
    monkeypatch.setattr(type(settings), "has_anthropic_key", property(lambda self: True))

    raw = client.get("/diagnostics").text

    assert "sk-clipboard-leak-test" not in raw
    assert "sk-" not in raw


def test_diagnostics_response_never_contains_the_session_secret(client, monkeypatch):
    monkeypatch.setenv("JARVIS_SESSION_SECRET", "ipc-secret-must-not-leak")
    assert "ipc-secret-must-not-leak" not in client.get("/diagnostics").text


def test_diagnostics_reports_loopback_only(client):
    assert "loopback only" in client.get("/diagnostics").json()["text"]


def test_diagnostics_includes_provider_availability_not_credentials(client):
    body = client.get("/diagnostics").json()
    providers = next(s for s in body["sections"] if s["title"] == "AI providers")
    values = " ".join(item["value"] for item in providers["items"])
    assert "sk-" not in values


# ---------------------------------------------------------------------------
# Settings page content
# ---------------------------------------------------------------------------

def test_settings_page_exposes_the_expected_controls(client):
    body = client.get("/ui/settings").text
    for element_id in (
        "settings-provider-list", "settings-key-status", "settings-key-input",
        "settings-key-save", "settings-key-remove", "settings-startup-toggle",
        "settings-privacy-status", "settings-paths",
    ):
        assert f'id="{element_id}"' in body


def test_settings_page_never_renders_a_key_server_side(client, monkeypatch):
    """CLAUDE.md forbids a template ever rendering the API key. The page
    shows a status only."""
    from app.config import settings
    monkeypatch.setattr(type(settings), "effective_api_key", property(lambda self: "sk-template-leak"))

    body = client.get("/ui/settings").text

    assert "sk-template-leak" not in body
    assert "ANTHROPIC_API_KEY" not in body


def test_settings_page_states_the_database_is_not_encrypted(client):
    """No false security claim — the same honesty as the wizard."""
    assert "not an encrypted vault" in client.get("/ui/settings").text


def test_settings_page_says_uninstall_preserves_data(client):
    assert "keeps this data" in client.get("/ui/settings").text


# ---------------------------------------------------------------------------
# Diagnostics page content
# ---------------------------------------------------------------------------

def test_diagnostics_page_has_copy_and_refresh(client):
    body = client.get("/ui/diagnostics").text
    assert 'id="diagnostics-copy"' in body
    assert 'id="diagnostics-refresh"' in body


def test_diagnostics_page_states_the_report_is_safe_to_share(client):
    body = client.get("/ui/diagnostics").text
    assert "never contains API keys" in body


# ---------------------------------------------------------------------------
# JS safety rules
# ---------------------------------------------------------------------------

def test_settings_and_diagnostics_js_never_use_inner_html():
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("// ── Settings page")
    end = js.index("// ── First run ─")
    assert "innerHTML" not in js[start:end]


def test_startup_toggle_trusts_the_server_reported_state():
    js = APP_JS.read_text(encoding="utf-8")
    assert "startup.checked = r.enabled" in js


def test_copy_failure_is_reported_rather_than_silent():
    """Clipboard access can be denied by the browser; the user must be
    told instead of clicking a button that appears to do nothing."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "Could not copy automatically" in js
