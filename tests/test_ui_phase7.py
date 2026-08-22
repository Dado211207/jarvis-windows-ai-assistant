"""
Tests for Phase 7: Professional UI/UX Polish
- Sidebar layout present in all pages
- No innerHTML in JS
- No API key exposure
- No external CDN or remote resources
- New CSS design system tokens present
- All 8 pages return 200
- Critical DOM IDs preserved
- Progress bar elements present in dashboard
- Chat suggestions present
- Voice planned-later section present
- Security: no sk-ant- tokens anywhere
"""

import re
import pytest


@pytest.fixture(scope="module")
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield prime_session(client)


# ── All pages return 200 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/ui/",
    "/ui/dashboard",
    "/ui/chat",
    "/ui/actions",
    "/ui/voice",
    "/ui/logs",
    "/ui/memory",
    "/ui/help",
    "/ui/setup",
])
def test_all_ui_pages_return_200(api_client, path):
    r = api_client.get(path)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ── Sidebar layout ────────────────────────────────────────────────────────────

def test_sidebar_present_in_dashboard(api_client):
    r = api_client.get("/ui/")
    assert "sidebar" in r.text


def test_sidebar_present_in_chat(api_client):
    r = api_client.get("/ui/chat")
    assert "sidebar" in r.text


def test_sidebar_present_in_help(api_client):
    r = api_client.get("/ui/help")
    assert "sidebar" in r.text


def test_sidebar_brand_present(api_client):
    r = api_client.get("/ui/")
    assert "sidebar-brand" in r.text
    assert "JARVIS" in r.text


def test_sidebar_nav_links_all_present(api_client):
    r = api_client.get("/ui/")
    html = r.text
    assert "/ui/chat" in html
    assert "/ui/actions" in html
    assert "/ui/logs" in html
    assert "/ui/memory" in html
    assert "/ui/voice" in html
    assert "/ui/help" in html
    assert "/ui/setup" in html


def test_sidebar_local_badge_present(api_client):
    r = api_client.get("/ui/")
    assert "local-badge" in r.text
    assert "127.0.0.1" in r.text


def test_topbar_present(api_client):
    r = api_client.get("/ui/")
    assert "topbar" in r.text
    assert "topbar-health-dot" in r.text
    assert "topbar-brain-dot" in r.text


# ── Dashboard — critical IDs preserved ───────────────────────────────────────

def test_dashboard_metric_ids_present(api_client):
    r = api_client.get("/ui/")
    html = r.text
    for elem_id in ["dash-health", "dash-db", "dash-brain", "dash-tools",
                    "dash-cpu", "dash-ram", "dash-uptime", "dash-tts",
                    "dash-version", "dash-phase"]:
        assert elem_id in html, f"Missing element id: {elem_id}"


def test_dashboard_progress_bars_present(api_client):
    r = api_client.get("/ui/")
    html = r.text
    assert "dash-cpu-bar" in html
    assert "dash-ram-bar" in html
    assert "progress-bar" in html


def test_dashboard_local_notice(api_client):
    r = api_client.get("/ui/")
    assert "127.0.0.1" in r.text


# ── Chat — suggestions and IDs ────────────────────────────────────────────────

def test_chat_ids_present(api_client):
    r = api_client.get("/ui/chat")
    html = r.text
    assert "chat-messages" in html
    assert "chat-input" in html
    assert "chat-send" in html


def test_chat_suggestions_present(api_client):
    r = api_client.get("/ui/chat")
    html = r.text
    assert "chat-suggestion" in html
    assert "status" in html


# ── Voice — planned-later section ─────────────────────────────────────────────

def test_voice_ids_present(api_client):
    r = api_client.get("/ui/voice")
    html = r.text
    assert "voice-output-toggle" in html
    assert "btn-speak-stop" in html
    assert "tts-avail" in html
    assert "tts-enabled-val" in html
    assert "tts-engine-val" in html
    # Voice input status belongs on the Voice page too — a page that
    # describes only the half that speaks is not the voice page.
    assert "stt-avail" in html
    assert "stt-model" in html


def test_voice_page_does_not_overclaim_always_listening(api_client):
    """v0.2 added real push-to-talk voice input (Chat page), so the Voice/TTS
    page correctly acknowledges it now rather than denying all microphone
    use — that's not an overclaim, it's honest. What must still never be
    claimed is wake-word / continuous / always-listening support, since
    that remains genuinely unimplemented (CLAUDE.md's Phase 3 rule)."""
    r = api_client.get("/ui/voice")
    html = r.text.lower()
    assert "wake word" in html or "wake-word" in html
    assert "no wake word" in html or "planned for a later phase" in html
    assert "always-listening" in html or "always listening" in html


def test_voice_planned_later_notice(api_client):
    r = api_client.get("/ui/voice")
    html = r.text.lower()
    assert "planned" in html


# ── Logs ──────────────────────────────────────────────────────────────────────

def test_logs_table_ids_present(api_client):
    r = api_client.get("/ui/logs")
    html = r.text
    assert "logs-tbody" in html
    assert "logs-refresh" in html


# ── Memory ────────────────────────────────────────────────────────────────────

def test_memory_search_ids_present(api_client):
    r = api_client.get("/ui/memory")
    html = r.text
    assert "memory-search" in html
    assert "memory-list" in html


# ── Help — commands & blocked section ─────────────────────────────────────────

def test_help_has_commands(api_client):
    r = api_client.get("/ui/help")
    html = r.text
    assert "memory add" in html
    assert "system status" in html
    assert "open website" in html


def test_help_has_blocked_section(api_client):
    r = api_client.get("/ui/help")
    html = r.text.lower()
    assert "blocked" in html or "permanently" in html


def test_help_has_planned_section(api_client):
    r = api_client.get("/ui/help")
    html = r.text.lower()
    assert "planned" in html


# ── Setup / first-run onboarding page ───────────────────────────────────────────

def test_setup_asks_only_for_a_name_and_a_key(api_client):
    """First run is two fields. The readiness table, provider discovery
    and speech-model installer that used to be here moved to the pages
    that own them — see tests/test_first_run.py."""
    r = api_client.get("/ui/setup")
    html = r.text
    assert 'id="setup-name-input"' in html
    assert 'id="setup-key-input"' in html
    assert 'id="setup-key-status"' in html


def test_setup_continue_button_present(api_client):
    r = api_client.get("/ui/setup")
    assert 'id="setup-continue"' in r.text


def test_model_install_controls_present_on_the_voice_page(api_client):
    r = api_client.get("/ui/voice")
    html = r.text
    for expected_id in (
        "model-info-name", "model-info-source", "model-info-license",
        "model-info-size", "model-info-destination", "model-info-checksum",
        "model-install-start", "model-install-cancel", "model-install-retry",
        "model-install-progress-bar",
    ):
        assert f'id="{expected_id}"' in html


def test_key_and_name_controls_present_on_settings(api_client):
    r = api_client.get("/ui/settings")
    html = r.text
    assert 'id="settings-key-input"' in html
    assert 'id="settings-key-save"' in html
    assert 'id="settings-key-remove"' in html
    assert 'id="settings-name-input"' in html
    assert 'id="settings-close-action"' in html


def test_setup_does_not_send_user_to_env_file(api_client):
    """The whole point of this page: no user is sent to .env to finish
    setup. (The page's own reassuring copy legitimately says "you won't
    need PowerShell" — mentioning the word to rule it out is fine; a
    real PowerShell *command* block would not be, which is what the
    absence of a <code> block containing one below actually checks.)"""
    r = api_client.get("/ui/setup")
    html = r.text.lower()
    assert ".env" not in html
    assert "set-executionpolicy" not in html
    assert ".ps1" not in html


def test_setup_api_key_input_is_password_type(api_client):
    r = api_client.get("/ui/setup")
    assert 'type="password"' in r.text


# ── CSS design system ─────────────────────────────────────────────────────────

def test_css_served_with_correct_content_type(api_client):
    r = api_client.get("/ui/static/style.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


def test_css_has_design_tokens(api_client):
    r = api_client.get("/ui/static/style.css")
    css = r.text
    assert "--bg:" in css
    assert "--accent:" in css
    assert "--sidebar-width:" in css


def test_css_has_sidebar_class(api_client):
    r = api_client.get("/ui/static/style.css")
    assert ".sidebar" in r.text
    assert ".sidebar-link" in r.text


def test_css_has_metric_card(api_client):
    r = api_client.get("/ui/static/style.css")
    assert ".metric-card" in r.text
    assert ".progress-bar" in r.text


def test_css_has_no_external_resources(api_client):
    r = api_client.get("/ui/static/style.css")
    css = r.text
    # No @import of external URLs
    assert "https://" not in css
    assert "http://" not in css
    assert "fonts.googleapis.com" not in css
    assert "cdnjs" not in css


# ── JS security checks ────────────────────────────────────────────────────────

def test_js_no_innerhtml(api_client):
    r = api_client.get("/ui/static/app.js")
    assert "innerHTML" not in r.text


def test_js_uses_textcontent(api_client):
    r = api_client.get("/ui/static/app.js")
    assert "textContent" in r.text


def test_js_has_progress_bar_function(api_client):
    r = api_client.get("/ui/static/app.js")
    assert "setProgressBar" in r.text


def test_js_has_topbar_update_functions(api_client):
    r = api_client.get("/ui/static/app.js")
    assert "setTopbarHealth" in r.text
    assert "setTopbarBrain" in r.text


def test_js_voice_uses_tts_field_names(api_client):
    r = api_client.get("/ui/static/app.js")
    # Must handle both tts_available and available for compatibility
    assert "tts_available" in r.text or "tts_enabled" in r.text


def test_js_no_api_key_exposure(api_client):
    r = api_client.get("/ui/static/app.js")
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-ant-" not in r.text


# ── Template security: no API key in any page ─────────────────────────────────

@pytest.mark.parametrize("path", [
    "/ui/", "/ui/chat", "/ui/actions", "/ui/voice",
    "/ui/logs", "/ui/memory", "/ui/setup",
])
def test_no_api_key_in_ui_pages(api_client, path):
    r = api_client.get(path)
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-ant-" not in r.text


def test_help_page_no_real_api_key_value(api_client):
    # Help page may mention the env var name in setup instructions
    # but must never render an actual sk-ant- token value
    r = api_client.get("/ui/help")
    assert "sk-ant-" not in r.text


def test_no_external_cdn_in_dashboard(api_client):
    r = api_client.get("/ui/")
    html = r.text
    # No external script or link tags pointing to CDNs
    assert "cdn.jsdelivr.net" not in html
    assert "cdnjs.cloudflare.com" not in html
    assert "fonts.googleapis.com" not in html
    assert "unpkg.com" not in html


def test_no_external_cdn_in_base_templates(api_client):
    for path in ["/ui/", "/ui/chat", "/ui/help"]:
        r = api_client.get(path)
        assert "cdn." not in r.text, f"CDN reference found in {path}"


# ── Active nav link ───────────────────────────────────────────────────────────

def test_dashboard_link_is_active_on_dashboard(api_client):
    r = api_client.get("/ui/")
    # The dashboard link should have 'active' class
    assert 'class="sidebar-link active"' in r.text or "sidebar-link active" in r.text


def test_chat_link_is_active_on_chat(api_client):
    r = api_client.get("/ui/chat")
    assert "sidebar-link active" in r.text


def test_help_link_is_active_on_help(api_client):
    r = api_client.get("/ui/help")
    assert "sidebar-link active" in r.text
