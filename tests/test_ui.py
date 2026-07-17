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
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield client


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


def test_ui_voice_has_controls(api_client):
    r = api_client.get("/ui/voice")
    html = r.text
    assert "btn-speak-on" in html
    assert "btn-speak-off" in html
    assert "btn-speak-stop" in html


def test_ui_help_has_commands(api_client):
    r = api_client.get("/ui/help")
    html = r.text
    assert "memory add" in html
    assert "system status" in html


def test_ui_help_points_to_settings_not_manual_env_editing(api_client):
    """Help must not tell users to hand-edit .env/.env.example — Settings'
    AI Provider section is the real path now (dev mode still uses .env, but
    that's not what an end user reading Help needs to be told)."""
    r = api_client.get("/ui/help")
    html = r.text
    assert "/ui/settings" in html
    assert ".env.example" not in html
    assert "text editor" not in html
    assert "START_JARVIS_API.bat" not in html


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


def test_ui_page_delivers_token_via_data_attribute_not_inline_script(api_client):
    """The session token must reach the page as a data-* attribute on
    <body>, not an inline <script> block — so the CSP's script-src can stay
    'self' only, with no unsafe-inline allowance (see app/api/local_guard.py's
    security headers and docs/SECURITY.md)."""
    from app.core import session_token

    r = api_client.get("/ui/")
    html = r.text
    token = session_token.get_token()
    assert f'data-jarvis-token="{token}"' in html
    assert "<script>" not in html
    assert "__JARVIS_TOKEN__" not in html


def test_onboarding_page_delivers_token_via_data_attribute_not_inline_script(api_client):
    from app.core import session_token

    r = api_client.get("/ui/onboarding")
    html = r.text
    token = session_token.get_token()
    assert f'data-jarvis-token="{token}"' in html
    assert "<script>" not in html
    assert "__JARVIS_TOKEN__" not in html


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
    assert "Phase" in body["phase"]


def test_health_stays_minimal(api_client):
    """/health is the one endpoint reachable without a session token — it
    must never carry DB status, provider status, or anything else beyond
    process-is-up (see app/api/local_guard.py's is_public_path)."""
    r = api_client.get("/health")
    body = r.json()
    assert set(body.keys()) == {"status", "healthy", "version", "phase"}


def test_ui_chat_post_command_roundtrip(api_client):
    r = api_client.post("/command", json={"command": "status"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
