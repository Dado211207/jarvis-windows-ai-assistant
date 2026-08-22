"""Tests for GET /onboarding/readiness and GET/POST /onboarding/complete
(app/api/routes.py)."""

from unittest.mock import patch

import pytest


@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield prime_session(client)


def test_readiness_returns_200_with_all_expected_keys(api_client):
    r = api_client.get("/onboarding/readiness")
    assert r.status_code == 200
    body = r.json()
    for key in (
        "core", "text_chat", "ai_provider", "mode", "stt_runtime",
        "speech_model", "tts", "database", "windows_automation",
    ):
        assert key in body
        assert "ready" in body[key]
        assert "detail" in body[key]


def test_readiness_no_auth_required(api_client):
    """Read-only status, like /privacy/status and /voice/stt-status —
    no session token needed."""
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as unprimed_client:
        r = unprimed_client.get("/onboarding/readiness")
    assert r.status_code == 200


def test_core_and_text_chat_always_ready(api_client):
    body = api_client.get("/onboarding/readiness").json()
    assert body["core"]["ready"] is True
    assert body["text_chat"]["ready"] is True


def test_ai_provider_and_mode_reflect_configured_key(api_client):
    with patch("app.config.settings.anthropic_api_key", "sk-ant-configured"):
        body = api_client.get("/onboarding/readiness").json()
    assert body["ai_provider"]["ready"] is True
    assert "cloud" in body["mode"]["detail"].lower()


def test_ai_provider_and_mode_reflect_unconfigured_key(api_client):
    with patch("app.config.settings.anthropic_api_key", ""), \
         patch("app.core.credentials.get_stored_api_key", return_value=""):
        body = api_client.get("/onboarding/readiness").json()
    assert body["ai_provider"]["ready"] is False
    assert "local" in body["mode"]["detail"].lower()


def test_mode_is_always_ready_even_when_local_only(api_client):
    """Local-only is a legitimate working mode, not a failure state."""
    with patch("app.config.settings.anthropic_api_key", ""), \
         patch("app.core.credentials.get_stored_api_key", return_value=""):
        body = api_client.get("/onboarding/readiness").json()
    assert body["mode"]["ready"] is True


def test_stt_and_speech_model_reflect_service_status(api_client):
    with patch("app.voice.stt.stt_service.is_available", return_value=(True, "ready")), \
         patch("app.voice.stt.stt_service.model_status", return_value=(True, "model at /x")):
        body = api_client.get("/onboarding/readiness").json()
    assert body["stt_runtime"] == {"ready": True, "detail": "ready"}
    assert body["speech_model"] == {"ready": True, "detail": "model at /x"}


def test_tts_reflects_service_availability(api_client):
    with patch("app.voice.tts.tts_service.is_available", return_value=False):
        body = api_client.get("/onboarding/readiness").json()
    assert body["tts"]["ready"] is False


def test_database_ready_when_reachable(api_client):
    body = api_client.get("/onboarding/readiness").json()
    assert body["database"]["ready"] is True


def test_database_not_ready_when_query_raises(api_client):
    with patch("db.database.get_db", side_effect=RuntimeError("db is locked")):
        body = api_client.get("/onboarding/readiness").json()
    assert body["database"]["ready"] is False


def test_windows_automation_always_reports_ready_with_platform_specific_detail(api_client):
    with patch("platform.system", return_value="Windows"):
        body = api_client.get("/onboarding/readiness").json()
    assert body["windows_automation"]["ready"] is True
    assert "windows" in body["windows_automation"]["detail"].lower()

    with patch("platform.system", return_value="Linux"):
        body = api_client.get("/onboarding/readiness").json()
    assert body["windows_automation"]["ready"] is True
    assert "fallback" in body["windows_automation"]["detail"].lower()


def test_readiness_never_exposes_the_api_key_value(api_client):
    with patch("app.config.settings.anthropic_api_key", "sk-ant-should-not-leak"):
        r = api_client.get("/onboarding/readiness")
    assert "sk-ant-should-not-leak" not in r.text


# ---------------------------------------------------------------------------
# GET/POST /onboarding/complete
# ---------------------------------------------------------------------------

def test_get_complete_status_reflects_module_state(api_client):
    with patch("app.core.onboarding.is_onboarding_complete", return_value=False):
        r = api_client.get("/onboarding/complete")
    assert r.status_code == 200
    assert r.json() == {"success": False}

    with patch("app.core.onboarding.is_onboarding_complete", return_value=True):
        r = api_client.get("/onboarding/complete")
    assert r.json() == {"success": True}


def test_post_complete_requires_session_token():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as unprimed_client:
        r = unprimed_client.post("/onboarding/complete")
    assert r.status_code == 403


def test_post_complete_marks_it_done(api_client):
    with patch("app.core.onboarding.mark_onboarding_complete") as mock_mark:
        r = api_client.post("/onboarding/complete")
    assert r.status_code == 200
    assert r.json() == {"success": True}
    mock_mark.assert_called_once()
