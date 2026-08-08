"""Tests for the /settings/api-key* endpoints (app/api/routes.py).

app.core.credentials is mocked here — it has its own dedicated test
file (tests/test_credentials.py) exercising the real (fake-backend)
storage logic. This file only proves the routes wire session-token
protection correctly, never echo the submitted key back, and translate
credentials.py's bool results into the right response shape.
"""

from unittest.mock import patch

import pytest


@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield prime_session(client)


# ---------------------------------------------------------------------------
# GET /settings/api-key-status
# ---------------------------------------------------------------------------

def test_status_not_configured_when_no_key_anywhere(api_client):
    with patch("app.config.settings.anthropic_api_key", ""), \
         patch("app.core.credentials.get_stored_api_key", return_value=""):
        r = api_client.get("/settings/api-key-status")
    assert r.status_code == 200
    assert r.json() == {"configured": False}


def test_status_configured_when_env_var_set(api_client):
    with patch("app.config.settings.anthropic_api_key", "sk-ant-from-env"):
        r = api_client.get("/settings/api-key-status")
    assert r.status_code == 200
    assert r.json() == {"configured": True}


def test_status_configured_when_only_credential_store_has_it(api_client):
    with patch("app.config.settings.anthropic_api_key", ""), \
         patch("app.core.credentials.get_stored_api_key", return_value="sk-ant-from-store"):
        r = api_client.get("/settings/api-key-status")
    assert r.status_code == 200
    assert r.json() == {"configured": True}


def test_status_response_never_contains_the_key_value(api_client):
    with patch("app.config.settings.anthropic_api_key", ""), \
         patch("app.core.credentials.get_stored_api_key", return_value="sk-ant-secret-value"):
        r = api_client.get("/settings/api-key-status")
    assert "sk-ant-secret-value" not in r.text


# ---------------------------------------------------------------------------
# POST /settings/api-key
# ---------------------------------------------------------------------------

def test_set_api_key_requires_session_token():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as unprimed_client:
        r = unprimed_client.post("/settings/api-key", json={"api_key": "sk-ant-x"})
    assert r.status_code == 403


def test_set_api_key_success(api_client):
    with patch("app.core.credentials.set_stored_api_key", return_value=True) as mock_set:
        r = api_client.post("/settings/api-key", json={"api_key": "sk-ant-new-key"})
    assert r.status_code == 200
    assert r.json()["success"] is True
    mock_set.assert_called_once_with("sk-ant-new-key")


def test_set_api_key_never_echoes_the_submitted_key(api_client):
    with patch("app.core.credentials.set_stored_api_key", return_value=True):
        r = api_client.post("/settings/api-key", json={"api_key": "sk-ant-do-not-leak-me"})
    assert "sk-ant-do-not-leak-me" not in r.text


def test_set_api_key_rejects_blank_key(api_client):
    r = api_client.post("/settings/api-key", json={"api_key": "   "})
    assert r.status_code == 422


def test_set_api_key_reports_failure_when_store_write_fails(api_client):
    with patch("app.core.credentials.set_stored_api_key", return_value=False):
        r = api_client.post("/settings/api-key", json={"api_key": "sk-ant-x"})
    assert r.status_code == 200
    assert r.json()["success"] is False


def test_set_api_key_strips_whitespace_before_storing(api_client):
    with patch("app.core.credentials.set_stored_api_key", return_value=True) as mock_set:
        api_client.post("/settings/api-key", json={"api_key": "  sk-ant-padded  "})
    mock_set.assert_called_once_with("sk-ant-padded")


# ---------------------------------------------------------------------------
# POST /settings/api-key/remove
# ---------------------------------------------------------------------------

def test_remove_api_key_requires_session_token():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as unprimed_client:
        r = unprimed_client.post("/settings/api-key/remove")
    assert r.status_code == 403


def test_remove_api_key_success(api_client):
    with patch("app.core.credentials.clear_stored_api_key", return_value=True) as mock_clear:
        r = api_client.post("/settings/api-key/remove")
    assert r.status_code == 200
    assert r.json()["success"] is True
    mock_clear.assert_called_once()


def test_remove_api_key_reports_failure_when_store_unreachable(api_client):
    with patch("app.core.credentials.clear_stored_api_key", return_value=False):
        r = api_client.post("/settings/api-key/remove")
    assert r.status_code == 200
    assert r.json()["success"] is False
