"""API tests for provider discovery and the start-with-Windows toggle.

Both are surfaces the onboarding wizard renders directly, so the
properties that matter are: never leak a credential, never claim an
undetected capability, and report the real resulting state of a toggle
rather than the requested one.
"""

import pytest
from fastapi.testclient import TestClient

from app.api.server import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _token(client) -> dict:
    """Mutating endpoints require the session token — see
    app/api/session.py's double-submit cookie."""
    client.get("/health")
    token = client.cookies.get("jarvis_session")
    return {"X-JARVIS-Session-Token": token} if token else {}


# ---------------------------------------------------------------------------
# GET /providers
# ---------------------------------------------------------------------------

def test_providers_lists_both_known_providers(client):
    body = client.get("/providers").json()
    names = {p["name"] for p in body["providers"]}
    assert names == {"anthropic", "ollama"}


def test_providers_reports_a_selected_provider(client):
    assert client.get("/providers").json()["selected"] in ("anthropic", "ollama")


def test_providers_never_returns_an_api_key(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(type(settings), "effective_api_key", property(lambda self: "sk-leaky-value-here"))
    monkeypatch.setattr(type(settings), "has_anthropic_key", property(lambda self: True))

    raw = client.get("/providers").text

    assert "sk-" not in raw
    assert "leaky" not in raw


def test_providers_does_not_claim_ollama_without_detection(client):
    """No Ollama server runs in CI, so it must be reported unavailable
    with an empty model list — never optimistically available."""
    body = client.get("/providers").json()
    ollama = next(p for p in body["providers"] if p["name"] == "ollama")

    assert ollama["available"] is False
    assert ollama["models"] == []


def test_providers_is_readable_without_a_session_token(client):
    """Read-only status must not require the mutation token."""
    assert client.get("/providers").status_code == 200


# ---------------------------------------------------------------------------
# Start with Windows
# ---------------------------------------------------------------------------

def test_startup_status_is_readable(client):
    body = client.get("/settings/startup").json()
    assert set(body) == {"supported", "enabled", "detail"}


def test_startup_reports_unsupported_off_windows(client):
    """This suite runs on Linux; the endpoint must say so plainly rather
    than pretend the feature exists."""
    body = client.get("/settings/startup").json()
    assert body["supported"] is False
    assert body["enabled"] is False
    assert "only available on Windows" in body["detail"]


def test_enabling_startup_reports_the_real_resulting_state(client):
    """The response must reflect what actually happened on disk. Off
    Windows nothing can be created, so enabled must stay False rather
    than echoing the request back as success."""
    response = client.post("/settings/startup", json={"enabled": True}, headers=_token(client))

    assert response.status_code == 200
    assert response.json()["enabled"] is False


def test_startup_toggle_requires_the_session_token(client):
    """A state-changing endpoint must stay behind the existing mutation
    protection."""
    response = client.post("/settings/startup", json={"enabled": True})
    assert response.status_code in (401, 403)


def test_startup_toggle_rejects_a_malformed_body(client):
    response = client.post("/settings/startup", json={"enabled": "yes please"}, headers=_token(client))
    assert response.status_code == 422
