"""
Tests for Phase 4: Local Browser Dashboard
- UI page routes (HTML responses)
- Static file serving (CSS, JS)
- New API endpoints: /system, /conversation
- Security: no API key in HTML, no innerHTML risk
"""

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(scope="module")
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield prime_session(client)


# ── UI page routes ────────────────────────────────────────────────────────────

def test_ui_dashboard_returns_200_html(api_client):
    r = api_client.get("/ui/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_ui_dashboard_alias_returns_200(api_client):
    r = api_client.get("/ui/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_ui_chat_returns_200_html(api_client):
    r = api_client.get("/ui/chat")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_ui_logs_returns_200_html(api_client):
    r = api_client.get("/ui/logs")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_ui_memory_returns_200_html(api_client):
    r = api_client.get("/ui/memory")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_ui_voice_returns_200_html(api_client):
    r = api_client.get("/ui/voice")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_ui_help_returns_200_html(api_client):
    r = api_client.get("/ui/help")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ── Static file serving ───────────────────────────────────────────────────────

def test_ui_static_css_served(api_client):
    r = api_client.get("/ui/static/style.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    assert "--bg:" in r.text  # CSS custom properties present


def test_ui_static_js_served(api_client):
    r = api_client.get("/ui/static/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "API.post" in r.text


# ── HTML structure / navigation ───────────────────────────────────────────────

def test_ui_nav_links_present(api_client):
    r = api_client.get("/ui/")
    html = r.text
    assert "/ui/chat" in html
    assert "/ui/logs" in html
    assert "/ui/memory" in html
    assert "/ui/voice" in html
    assert "/ui/help" in html


def test_ui_dashboard_has_expected_elements(api_client):
    r = api_client.get("/ui/")
    html = r.text
    assert "dash-health" in html
    assert "dash-db" in html
    assert "dash-brain" in html
    assert "dash-cpu" in html
    assert "dash-ram" in html


def test_ui_dashboard_safety_notice(api_client):
    r = api_client.get("/ui/")
    html = r.text
    assert "127.0.0.1" in html


def test_ui_chat_has_input_and_send(api_client):
    r = api_client.get("/ui/chat")
    html = r.text
    assert "chat-input" in html
    assert "chat-send" in html
    assert "chat-messages" in html


def test_ui_logs_has_table(api_client):
    r = api_client.get("/ui/logs")
    html = r.text
    assert "logs-tbody" in html
    assert "logs-refresh" in html


def test_ui_memory_has_search(api_client):
    r = api_client.get("/ui/memory")
    html = r.text
    assert "memory-search" in html
    assert "memory-list" in html


def test_ui_voice_offers_the_neural_voice_controls(api_client):
    """The page has to let someone install the voice, choose it, hear it
    and fix how their name is said — the reported defect was a robotic
    voice with no control over any of that."""
    html = api_client.get("/ui/voice").text

    for element in (
        "nv-engine-list",       # every tier's own reason
        "nv-voice-select",      # which voice
        "nv-test",              # hear it
        "nv-install-start",     # install it
        "nv-install-cancel",    # and stop installing it
        "nv-progress-bar",      # with progress
        "pron-word",            # teach it a name
        "nv-name-prompt",       # and be told when it needs one
    ):
        assert element in html, f"the Voice page is missing {element}"


def test_ui_voice_does_not_claim_an_engine_that_was_removed(api_client):
    """espeak is deliberately not in this product — the page said it
    was, which was both wrong and a licence claim.

    Checked against the rendered page rather than the template file so
    it also covers anything a later include might reintroduce.
    """
    html = api_client.get("/ui/voice").text.lower()

    assert "espeak" not in html
    assert "pyttsx3" not in html


def test_ui_voice_has_controls(api_client):
    r = api_client.get("/ui/voice")
    html = r.text
    # A single remembered toggle replaced the old Enable/Disable button
    # pair, which set a flag nothing on this page ever read back.
    assert "voice-output-toggle" in html
    assert "btn-speak-test" in html
    assert "btn-speak-stop" in html


def test_ui_help_has_commands(api_client):
    r = api_client.get("/ui/help")
    html = r.text
    assert "memory add" in html
    assert "system status" in html


# ── Security: no API key exposure ────────────────────────────────────────────

def test_ui_no_api_key_in_dashboard(api_client):
    r = api_client.get("/ui/")
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-" not in r.text


def test_ui_no_api_key_in_chat(api_client):
    r = api_client.get("/ui/chat")
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-" not in r.text


def test_ui_js_uses_textcontent_not_innerhtml(api_client):
    r = api_client.get("/ui/static/app.js")
    js = r.text
    # innerHTML must not be used for user content
    assert "innerHTML" not in js


# ── New API endpoints ─────────────────────────────────────────────────────────

def test_system_endpoint_returns_200(api_client):
    r = api_client.get("/system")
    assert r.status_code == 200


def test_system_endpoint_has_required_fields(api_client):
    r = api_client.get("/system")
    body = r.json()
    assert "cpu_percent" in body
    assert "ram_percent" in body
    assert "uptime" in body
    assert "tools_registered" in body
    assert "version" in body
    assert "phase" in body


def test_system_endpoint_tools_registered_positive(api_client):
    r = api_client.get("/system")
    body = r.json()
    assert body["tools_registered"] >= 9


def test_system_endpoint_no_api_key(api_client):
    r = api_client.get("/system")
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-" not in r.text


def test_conversation_endpoint_returns_200(api_client):
    r = api_client.get("/conversation")
    assert r.status_code == 200


def test_conversation_endpoint_returns_list(api_client):
    r = api_client.get("/conversation")
    body = r.json()
    assert isinstance(body, list)


def test_health_includes_phase(api_client):
    r = api_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "phase" in body
    assert body["phase"]  # non-empty; naming convention may evolve (e.g. "v0.2: ...")


def test_health_includes_db_string(api_client):
    r = api_client.get("/health")
    body = r.json()
    assert body["db"] in ("ok", "error")


def test_ui_chat_post_command_roundtrip(api_client):
    r = api_client.post("/command", json={"command": "status"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
