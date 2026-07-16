"""Tests for the first-run onboarding wizard (backend + API + UI gating).

Onboarding only ever matters for a frozen build, so every test isolates
production paths the same way tests/test_paths.py and tests/test_secret_store.py
do: JARVIS_APPDATA_OVERRIDE + a tmp_path chdir, so nothing here ever touches
the real repo's data/ directory or a real user profile.
"""

from unittest.mock import patch

import httpx
import pytest

from app.core import onboarding


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JARVIS_APPDATA_OVERRIDE", str(tmp_path))
    onboarding.reset_state_for_tests()
    yield
    onboarding.reset_state_for_tests()


def _req():
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


# --- is_required / is_complete ---

def test_not_required_in_dev_mode():
    assert onboarding.is_required() is False


def test_required_when_frozen_and_incomplete():
    with patch("app.core.onboarding.paths.is_frozen", return_value=True):
        assert onboarding.is_required() is True


def test_not_required_once_complete():
    with patch("app.core.onboarding.paths.is_frozen", return_value=True):
        onboarding.skip_api_key()
        result = onboarding.complete()
        assert result["success"] is True
        assert onboarding.is_required() is False


# --- state / steps ---

def test_get_state_shape():
    state = onboarding.get_state()
    assert state["step"] == "welcome"
    assert state["steps"] == list(onboarding.STEPS)
    assert state["api_key_status"] == "not_set"
    assert state["complete"] is False


def test_set_step_valid():
    assert onboarding.set_step("privacy")["success"] is True
    assert onboarding.get_state()["step"] == "privacy"


def test_set_step_invalid_rejected():
    result = onboarding.set_step("not-a-real-step")
    assert result["success"] is False
    assert onboarding.get_state()["step"] == "welcome"


# --- API key: shape validation ---

@pytest.mark.parametrize("bad", ["", "not-a-key", "sk-ant-short", "   "])
def test_submit_api_key_rejects_bad_shape(bad):
    result = onboarding.submit_api_key(bad)
    assert result["success"] is False
    assert onboarding.get_state()["api_key_status"] == "invalid"


# --- API key: live validation + storage ---

def test_submit_api_key_success():
    with patch("app.core.onboarding._validate_with_anthropic", return_value=(True, None)), \
         patch("app.core.secret_store.save_api_key") as mock_save:
        result = onboarding.submit_api_key("sk-ant-abcdefghijklmnop")
    assert result["success"] is True
    mock_save.assert_called_once_with("sk-ant-abcdefghijklmnop")
    assert onboarding.get_state()["api_key_status"] == "validated"


def test_submit_api_key_validation_rejected():
    with patch("app.core.onboarding._validate_with_anthropic",
               return_value=(False, "That API key was rejected by Anthropic. Double-check it and try again.")):
        result = onboarding.submit_api_key("sk-ant-abcdefghijklmnop")
    assert result["success"] is False
    assert "rejected" in result["error"]
    assert onboarding.get_state()["api_key_status"] == "invalid"


def test_submit_api_key_save_failure_surfaces_error():
    from app.core.secret_store import SecretStoreError
    with patch("app.core.onboarding._validate_with_anthropic", return_value=(True, None)), \
         patch("app.core.secret_store.save_api_key", side_effect=SecretStoreError("boom")):
        result = onboarding.submit_api_key("sk-ant-abcdefghijklmnop")
    assert result["success"] is False
    assert onboarding.get_state()["api_key_status"] == "invalid"


def test_submit_api_key_never_logs_raw_key(caplog):
    caplog.set_level("DEBUG")
    with patch("app.core.onboarding._validate_with_anthropic",
               return_value=(False, "Could not validate the API key right now. Try again, or skip for now.")):
        onboarding.submit_api_key("sk-ant-verysecretvalue12345")
    assert "verysecretvalue12345" not in caplog.text


def test_skip_api_key():
    result = onboarding.skip_api_key()
    assert result["success"] is True
    assert onboarding.get_state()["api_key_status"] == "skipped"


def test_remove_api_key():
    with patch("app.core.secret_store.delete_api_key") as mock_delete:
        result = onboarding.remove_api_key()
    assert result["success"] is True
    mock_delete.assert_called_once()
    assert onboarding.get_state()["api_key_status"] == "not_set"


# --- error classification (real anthropic exception types) ---

def test_classify_authentication_error():
    resp = httpx.Response(401, request=_req())
    import anthropic
    exc = anthropic.AuthenticationError("bad key", response=resp, body=None)
    assert "rejected" in onboarding._classify_error(exc)


def test_classify_rate_limit_error():
    resp = httpx.Response(429, request=_req())
    import anthropic
    exc = anthropic.RateLimitError("slow down", response=resp, body=None)
    assert "rate limit" in onboarding._classify_error(exc)


def test_classify_connection_error():
    import anthropic
    exc = anthropic.APIConnectionError(request=_req())
    assert "internet connection" in onboarding._classify_error(exc)


def test_classify_status_error():
    resp = httpx.Response(500, request=_req())
    import anthropic
    exc = anthropic.APIStatusError("down", response=resp, body=None)
    assert "unavailable" in onboarding._classify_error(exc)


def test_classify_unknown_error_has_generic_message():
    assert "Could not validate" in onboarding._classify_error(RuntimeError("weird"))


# --- voice / startup preferences persist via settings_service ---

@pytest.fixture
def _settings_db(tmp_path, monkeypatch):
    from app.config import settings as app_settings
    import db.database as dbmod

    old_path = app_settings.jarvis_db_path
    app_settings.jarvis_db_path = str(tmp_path / "onboarding_test.db")
    dbmod._db_instance = None
    from db.migrations import create_tables
    create_tables()
    yield
    dbmod._db_instance = None
    app_settings.jarvis_db_path = old_path


def test_set_voice_preference_persists(_settings_db):
    from app.core import settings_service
    result = onboarding.set_voice_preference(True)
    assert result["success"] is True
    assert settings_service.get("tts_enabled") == "true"


def test_set_startup_preference_persists(_settings_db):
    from app.core import settings_service
    result = onboarding.set_startup_preference(True)
    assert result["success"] is True
    assert settings_service.get("start_with_windows") == "true"


# --- complete() refuses on unresolved API key step ---

def test_complete_refuses_when_api_key_unresolved():
    result = onboarding.complete()
    assert result["success"] is False
    assert onboarding.is_complete() is False


def test_complete_succeeds_after_skip():
    onboarding.skip_api_key()
    result = onboarding.complete()
    assert result["success"] is True
    assert onboarding.is_complete() is True


def test_complete_succeeds_after_validated_key():
    with patch("app.core.onboarding._validate_with_anthropic", return_value=(True, None)), \
         patch("app.core.secret_store.save_api_key"):
        onboarding.submit_api_key("sk-ant-abcdefghijklmnop")
    result = onboarding.complete()
    assert result["success"] is True


def test_complete_never_marks_done_on_invalid_key():
    with patch("app.core.onboarding._validate_with_anthropic", return_value=(False, "nope")):
        onboarding.submit_api_key("sk-ant-abcdefghijklmnop")
    result = onboarding.complete()
    assert result["success"] is False
    assert onboarding.is_complete() is False


# --- API routes: onboarding_routes.py wires request/response correctly ---
# (underlying logic is exercised directly above; these mock it out so the
# routes stay focused on "does the wiring work" without real side effects)

@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield client


def test_onboarding_state_endpoint(api_client):
    r = api_client.get("/onboarding/state")
    assert r.status_code == 200
    body = r.json()
    assert "step" in body
    assert "steps" in body


def test_onboarding_step_endpoint(api_client):
    r = api_client.post("/onboarding/step", json={"step": "privacy"})
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_onboarding_api_key_endpoint_never_echoes_key(api_client):
    with patch("app.core.onboarding.submit_api_key", return_value={"success": True}) as mock_submit:
        r = api_client.post("/onboarding/api-key", json={"api_key": "sk-ant-verysecretvalue12345"})
    assert r.status_code == 200
    assert "verysecretvalue12345" not in r.text
    mock_submit.assert_called_once_with("sk-ant-verysecretvalue12345")


def test_onboarding_api_key_skip_endpoint(api_client):
    with patch("app.core.onboarding.skip_api_key", return_value={"success": True}):
        r = api_client.post("/onboarding/api-key/skip")
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_onboarding_api_key_delete_endpoint(api_client):
    with patch("app.core.onboarding.remove_api_key", return_value={"success": True}) as mock_remove:
        r = api_client.delete("/onboarding/api-key")
    assert r.status_code == 200
    mock_remove.assert_called_once()


def test_onboarding_voice_endpoint(api_client):
    with patch("app.core.onboarding.set_voice_preference", return_value={"success": True}) as mock_voice:
        r = api_client.post("/onboarding/voice", json={"enabled": True})
    assert r.status_code == 200
    mock_voice.assert_called_once_with(True)


def test_onboarding_startup_endpoint(api_client):
    with patch("app.core.onboarding.set_startup_preference", return_value={"success": True}) as mock_startup:
        r = api_client.post("/onboarding/startup", json={"enabled": False})
    assert r.status_code == 200
    mock_startup.assert_called_once_with(False)


def test_onboarding_complete_endpoint(api_client):
    with patch("app.core.onboarding.complete", return_value={"success": True}):
        r = api_client.post("/onboarding/complete")
    assert r.status_code == 200
    assert r.json()["success"] is True


# --- UI: onboarding page + first-run redirect gating ---

def test_ui_onboarding_page_returns_200(api_client):
    r = api_client.get("/ui/onboarding")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_ui_onboarding_no_secrets_in_html(api_client):
    r = api_client.get("/ui/onboarding")
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-" not in r.text


def test_ui_onboarding_js_uses_textcontent_not_innerhtml(api_client):
    r = api_client.get("/ui/static/onboarding.js")
    assert r.status_code == 200
    assert "innerHTML" not in r.text


def test_ui_static_onboarding_css_served(api_client):
    r = api_client.get("/ui/static/onboarding.css")
    assert r.status_code == 200


def test_ui_dashboard_redirects_when_onboarding_required(api_client):
    with patch("app.ui.routes.onboarding.is_required", return_value=True):
        r = api_client.get("/ui/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"].endswith("/ui/onboarding")


def test_ui_settings_redirects_when_onboarding_required(api_client):
    with patch("app.ui.routes.onboarding.is_required", return_value=True):
        r = api_client.get("/ui/settings", follow_redirects=False)
    assert r.status_code in (302, 307)


def test_ui_dashboard_no_redirect_when_not_required(api_client):
    with patch("app.ui.routes.onboarding.is_required", return_value=False):
        r = api_client.get("/ui/", follow_redirects=False)
    assert r.status_code == 200
