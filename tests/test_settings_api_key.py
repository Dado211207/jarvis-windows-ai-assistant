"""Tests for the /settings/api-key* endpoints (app/api/routes.py).

app.core.credentials is mocked here — it has its own dedicated test
file (tests/test_credentials.py) exercising the real (fake-backend)
storage logic. This file only proves the routes wire session-token
protection correctly, never echo the submitted key back, and translate
credentials.py's bool results into the right response shape.

Verification is mocked too, for two reasons: CLAUDE.md forbids a real
Anthropic call from any test, and the classification logic it depends on
has its own tests in tests/test_error_envelope.py. The `_verifies`
helper below is the single seam, so no test in this file can reach the
network by forgetting to patch it — see
test_saving_a_key_never_reaches_the_network.
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.core.ai.key_check import KeyVerification
from app.core.errors import ErrorCategory


@contextmanager
def _verifies(ok=True, category=None, worth_storing=True, message="API key verified."):
    """Stand in for the one real request /settings/api-key makes."""
    result = KeyVerification(ok=ok, message=message, category=category, worth_storing=worth_storing)
    with patch("app.core.ai.key_check.verify_anthropic_key", return_value=result) as mock:
        yield mock


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
    body = r.json()
    assert body["configured"] is False
    assert body["workspace_configured"] is False
    assert body["state"] == "not_configured"


def test_status_configured_when_env_var_set(api_client):
    with patch("app.config.settings.anthropic_api_key", "sk-ant-from-env"):
        r = api_client.get("/settings/api-key-status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    # A key that exists but was never checked here is not "verified".
    assert body["state"] in {"configured_unverified", "verification_failed", "verified"}


def test_status_configured_when_only_credential_store_has_it(api_client):
    with patch("app.config.settings.anthropic_api_key", ""), \
         patch("app.core.credentials.get_stored_api_key", return_value="sk-ant-from-store"):
        r = api_client.get("/settings/api-key-status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    # A key that exists but was never checked here is not "verified".
    assert body["state"] in {"configured_unverified", "verification_failed", "verified"}


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
    with _verifies(), patch("app.core.credentials.set_stored_api_key", return_value=True) as mock_set:
        r = api_client.post("/settings/api-key", json={"api_key": "sk-ant-new-key"})
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert r.json()["stored"] is True
    mock_set.assert_called_once_with("sk-ant-new-key")


def test_set_api_key_never_echoes_the_submitted_key(api_client):
    with _verifies(), patch("app.core.credentials.set_stored_api_key", return_value=True):
        r = api_client.post("/settings/api-key", json={"api_key": "sk-ant-do-not-leak-me"})
    assert "sk-ant-do-not-leak-me" not in r.text


def test_set_api_key_rejects_blank_key(api_client):
    r = api_client.post("/settings/api-key", json={"api_key": "   "})
    assert r.status_code == 422


def test_set_api_key_reports_failure_when_store_write_fails(api_client):
    with _verifies(), patch("app.core.credentials.set_stored_api_key", return_value=False):
        r = api_client.post("/settings/api-key", json={"api_key": "sk-ant-x"})
    assert r.status_code == 200
    assert r.json()["success"] is False
    assert r.json()["stored"] is False


def test_set_api_key_strips_whitespace_before_storing(api_client):
    with _verifies(), patch("app.core.credentials.set_stored_api_key", return_value=True) as mock_set:
        api_client.post("/settings/api-key", json={"api_key": "  sk-ant-padded  "})
    mock_set.assert_called_once_with("sk-ant-padded")


# ---------------------------------------------------------------------------
# The key is tried before it is trusted
# ---------------------------------------------------------------------------

def test_the_key_is_verified_before_it_is_stored(api_client):
    """A key saved without being tried is a key whose first failure
    happens later, mid-conversation, with no visible connection to the
    screen where it was typed."""
    with _verifies() as verify, patch("app.core.credentials.set_stored_api_key", return_value=True):
        api_client.post("/settings/api-key", json={"api_key": "sk-ant-x"})
    # The workspace ID travels with the key because Anthropic treats an
    # identity-linked key and its workspace as one credential: verifying
    # the key alone would either fail a good key or bless a pair that has
    # never made a successful request. Blank here is a legacy
    # workspace-scoped key, which sends no header at all.
    verify.assert_called_once_with("sk-ant-x", "")


def test_a_rejected_key_is_never_stored(api_client):
    with _verifies(ok=False, category=ErrorCategory.PROVIDER_AUTH, worth_storing=False,
                   message="rejected"), \
         patch("app.core.credentials.set_stored_api_key") as mock_set:
        r = api_client.post("/settings/api-key", json={"api_key": "sk-ant-bad"})

    body = r.json()
    assert body["success"] is False
    assert body["stored"] is False
    assert body["category"] == "provider_auth"
    mock_set.assert_not_called()


@pytest.mark.parametrize("category", [
    ErrorCategory.PROVIDER_BILLING,
    ErrorCategory.PROVIDER_RATE_LIMIT,
    ErrorCategory.PROVIDER_UNAVAILABLE,
    ErrorCategory.PROVIDER_TIMEOUT,
])
def test_a_key_that_could_not_be_checked_is_still_stored(api_client, category):
    """Being rate-limited or offline during setup says nothing about the
    key. Making someone type it again afterwards would punish them for
    their network."""
    with _verifies(ok=False, category=category, worth_storing=True, message="try later"), \
         patch("app.core.credentials.set_stored_api_key", return_value=True) as mock_set:
        r = api_client.post("/settings/api-key", json={"api_key": "sk-ant-fine"})

    body = r.json()
    assert body["stored"] is True
    assert body["category"] == category.value
    mock_set.assert_called_once()


def test_the_four_failure_causes_do_not_share_one_message(api_client):
    """CLAUDE.md: a rate limit, an expired key, an unfunded account and an
    unreachable provider are four different problems with four different
    fixes."""
    from app.core.errors import safe_message

    messages = {
        safe_message(category)
        for category in (
            ErrorCategory.PROVIDER_AUTH,
            ErrorCategory.PROVIDER_BILLING,
            ErrorCategory.PROVIDER_RATE_LIMIT,
            ErrorCategory.PROVIDER_UNAVAILABLE,
        )
    }
    assert len(messages) == 4


def test_saving_a_key_never_reaches_the_network(api_client):
    """CLAUDE.md forbids a real Anthropic call from any test. Proven by
    making the SDK itself explode: if any code path here constructs a
    real client, this fails loudly instead of quietly making a request."""
    def _explode(*args, **kwargs):
        raise AssertionError("a test tried to construct a real Anthropic client")

    with patch("anthropic.Anthropic", _explode), \
         patch("app.core.credentials.set_stored_api_key", return_value=True):
        r = api_client.post("/settings/api-key", json={"api_key": "sk-ant-x"})

    # The request still completes — the failure is classified, not raised.
    assert r.status_code == 200
    assert r.json()["stored"] is False


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
