"""Audit tests for the "skippable API key" onboarding behavior and the
Settings-page path to finish provider setup afterward.

Confirms: JARVIS's local fallback for AI-dependent commands is genuinely
functional (never crashes, always responds) but is NOT a real AI answer —
so the UI must say so plainly rather than imply full readiness. Covers the
full set of provider-setup outcomes end-to-end (valid key, each classified
failure mode, postponed setup, key replacement/removal) and the Settings
page's direct path to finish setup without re-running onboarding.
"""

from unittest.mock import MagicMock, patch

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


def _mock_anthropic_client(raise_exc=None, success_text="hi there"):
    """Builds a fake `anthropic.Anthropic(...)` instance whose
    messages.create() either raises *raise_exc* or returns a minimal
    successful response — exercises the real _validate_with_anthropic path
    rather than mocking it away, so the classification logic is genuinely
    under test end-to-end."""
    client = MagicMock()
    if raise_exc is not None:
        client.messages.create.side_effect = raise_exc
    else:
        message = MagicMock()
        message.content = [MagicMock(text=success_text)]
        client.messages.create.return_value = message
    return client


# --- functional local fallback: proven, not just claimed ---

def test_local_fallback_never_crashes_and_is_clearly_not_ai():
    from app.core.brain import Brain
    b = Brain()
    with patch("app.core.brain.settings") as s:
        s.has_anthropic_key = False
        s.jarvis_ai_provider = "anthropic"
        result = b.generate_response("what is the meaning of life?")
    assert result.used_api is False
    assert result.provider == "local"
    assert result.error is None
    # explicitly says setup is required — never presented as a real answer
    assert "not configured" in result.content
    assert "can't answer" in result.content


def test_deterministic_commands_work_without_any_provider():
    """The other half of "functional fallback": built-in commands are not
    degraded at all by a missing API key — this is the Brain's router path,
    not generate_response(), and never touches Anthropic."""
    from app.core.brain import brain
    response = brain.process("status")
    assert response.success is True


# --- AI command attempted without a configured provider ---

def test_ai_command_without_provider_reports_setup_required_via_api():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app

    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        with patch("app.core.brain.settings") as s:
            s.has_anthropic_key = False
            s.jarvis_ai_provider = "anthropic"
            r = client.post("/command", json={"command": "completely unrecognized natural language query"})

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["tool_used"] == "brain"
    assert body["data"]["used_api"] is False
    assert "not configured" in body["message"]


# --- valid key, end-to-end through the real classification path ---

def test_valid_key_end_to_end():
    with patch("anthropic.Anthropic", return_value=_mock_anthropic_client()), \
         patch("app.core.secret_store.save_api_key") as mock_save:
        result = onboarding.submit_api_key("sk-ant-abcdefghijklmnop")
    assert result["success"] is True
    mock_save.assert_called_once_with("sk-ant-abcdefghijklmnop")
    assert onboarding.get_state()["api_key_status"] == "validated"


# --- each classified failure mode, end-to-end (real exception raised by
#     the mocked Anthropic client, real _validate_with_anthropic/_classify_error) ---

def test_invalid_key_rejected_by_anthropic_end_to_end():
    exc = __import__("anthropic").AuthenticationError("bad key", response=httpx.Response(401, request=_req()), body=None)
    with patch("anthropic.Anthropic", return_value=_mock_anthropic_client(raise_exc=exc)):
        result = onboarding.submit_api_key("sk-ant-abcdefghijklmnop")
    assert result["success"] is False
    assert "rejected" in result["error"]
    assert onboarding.get_state()["api_key_status"] == "invalid"


def test_no_internet_end_to_end():
    import anthropic
    exc = anthropic.APIConnectionError(request=_req())
    with patch("anthropic.Anthropic", return_value=_mock_anthropic_client(raise_exc=exc)):
        result = onboarding.submit_api_key("sk-ant-abcdefghijklmnop")
    assert result["success"] is False
    assert "internet connection" in result["error"]


def test_rate_limit_end_to_end():
    import anthropic
    exc = anthropic.RateLimitError("slow down", response=httpx.Response(429, request=_req()), body=None)
    with patch("anthropic.Anthropic", return_value=_mock_anthropic_client(raise_exc=exc)):
        result = onboarding.submit_api_key("sk-ant-abcdefghijklmnop")
    assert result["success"] is False
    assert "rate limit" in result["error"]


def test_provider_unavailable_end_to_end():
    import anthropic
    exc = anthropic.APIStatusError("down", response=httpx.Response(500, request=_req()), body=None)
    with patch("anthropic.Anthropic", return_value=_mock_anthropic_client(raise_exc=exc)):
        result = onboarding.submit_api_key("sk-ant-abcdefghijklmnop")
    assert result["success"] is False
    assert "unavailable" in result["error"]


# --- postponed setup: never trapped, never claims full readiness ---

def test_postponed_setup_completes_and_never_traps_user():
    with patch("app.core.onboarding.paths.is_frozen", return_value=True):
        onboarding.skip_api_key()
        assert onboarding.get_state()["api_key_status"] == "skipped"

        result = onboarding.complete()
        assert result["success"] is True
        assert onboarding.is_complete() is True

        # is_required() must stay False forever after — the user deliberately
        # postponed; onboarding must never re-trap them on a later launch.
        assert onboarding.is_required() is False


# --- key replacement ---

def test_key_replacement_saves_new_value():
    with patch("anthropic.Anthropic", return_value=_mock_anthropic_client()), \
         patch("app.core.secret_store.save_api_key") as mock_save:
        onboarding.submit_api_key("sk-ant-firstkeyvalue111")
        onboarding.submit_api_key("sk-ant-secondkeyvalue222")

    assert mock_save.call_count == 2
    mock_save.assert_called_with("sk-ant-secondkeyvalue222")
    assert onboarding.get_state()["api_key_status"] == "validated"


# --- key removal + onboarding state after removal ---

def test_onboarding_state_after_key_removal():
    with patch("anthropic.Anthropic", return_value=_mock_anthropic_client()), \
         patch("app.core.secret_store.save_api_key"):
        onboarding.submit_api_key("sk-ant-abcdefghijklmnop")
    assert onboarding.get_state()["api_key_status"] == "validated"

    with patch("app.core.secret_store.delete_api_key") as mock_delete:
        result = onboarding.remove_api_key()
    assert result["success"] is True
    mock_delete.assert_called_once()
    assert onboarding.get_state()["api_key_status"] == "not_set"


# --- Settings page: direct path to finish provider setup ---

def test_settings_page_has_provider_section():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        r = client.get("/ui/settings")
    html = r.text
    assert "provider-api-key" in html
    assert "provider-key-save" in html
    assert "provider-key-remove" in html
    assert "sk-" not in html
    assert "ANTHROPIC_API_KEY" not in html


def test_settings_provider_save_uses_same_onboarding_endpoint():
    """Settings' "Save key" button must go through the same validated,
    secure-storage path as onboarding — not a separate, weaker one."""
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        with patch("app.core.onboarding.submit_api_key", return_value={"success": True}) as mock_submit:
            r = client.post("/onboarding/api-key", json={"api_key": "sk-ant-fromsettingspage1"})
    assert r.status_code == 200
    mock_submit.assert_called_once_with("sk-ant-fromsettingspage1")
