"""Tests for app/core/providers.py.

The governing rule is CLAUDE.md's onboarding requirement: never claim a
capability that was not actually detected. These tests are written to
catch a regression in that direction specifically — a provider reported
available when nothing real was checked, or a model list that was
invented rather than read from a live instance.
"""

from unittest.mock import MagicMock

import pytest

from app.core import providers


class _Response:
    def __init__(self, status_code=200, payload=None, raises=False):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


class _Client:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append({"url": url, "timeout": timeout})
        if self._error is not None:
            raise self._error
        return self._response


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

def test_anthropic_is_available_only_when_the_key_was_actually_checked(monkeypatch):
    """`available` means the provider answered — not that a key exists.

    This assertion used to read "available iff a key is configured", and
    that is exactly what went wrong on a real Windows 11 machine: an
    identity-linked key was stored, Anthropic rejected every request with
    HTTP 400 for a missing `anthropic-workspace-id` header, and the
    dashboard went on reporting "natural-language chat is available"
    because a credential was present. A key that exists and a key that
    works are different facts.
    """
    from app.config import settings
    monkeypatch.setattr(type(settings), "has_anthropic_key", property(lambda self: True))
    monkeypatch.setattr(providers, "anthropic_credential_state", lambda: providers.CREDENTIAL_VERIFIED)
    assert providers.anthropic_status().available is True

    # The defect, stated as an assertion: present but never checked is
    # not available, and neither is present but rejected.
    monkeypatch.setattr(providers, "anthropic_credential_state", lambda: providers.CREDENTIAL_UNVERIFIED)
    assert providers.anthropic_status().available is False

    monkeypatch.setattr(providers, "anthropic_credential_state", lambda: providers.CREDENTIAL_FAILED)
    assert providers.anthropic_status().available is False

    monkeypatch.setattr(providers, "anthropic_credential_state", lambda: providers.CREDENTIAL_NOT_CONFIGURED)
    assert providers.anthropic_status().available is False


def test_no_credential_state_claims_chat_is_available_except_the_verified_one(monkeypatch):
    """The sentence the dashboard renders, held to the same rule as the
    boolean beside it."""
    for state, detail in providers._CREDENTIAL_DETAIL.items():
        if state == providers.CREDENTIAL_VERIFIED:
            continue
        assert "is available" not in detail, (
            f"the {state!r} detail tells the user chat works: {detail!r}"
        )


def test_anthropic_status_never_contains_the_key(monkeypatch):
    """A status object is rendered into the UI; it must carry a boolean,
    never the credential."""
    from app.config import settings
    monkeypatch.setattr(type(settings), "effective_api_key", property(lambda self: "sk-super-secret-value"))
    monkeypatch.setattr(type(settings), "has_anthropic_key", property(lambda self: True))

    status = providers.anthropic_status()

    assert "sk-" not in status.detail
    assert "sk-" not in repr(status)


def test_anthropic_says_jarvis_still_works_without_a_key(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(type(settings), "has_anthropic_key", property(lambda self: False))
    assert "without one" in providers.anthropic_status().detail


# ---------------------------------------------------------------------------
# Ollama — only ever available when a real instance answered
# ---------------------------------------------------------------------------

def test_ollama_unavailable_when_nothing_is_listening():
    """The common case for most users. Must be an ordinary negative
    result, not an error."""
    client = _Client(error=ConnectionRefusedError("nothing there"))
    status = providers.ollama_status(http_client=client)

    assert status.available is False
    assert status.models == []
    assert "No local Ollama server detected" in status.detail


def test_ollama_unavailable_on_a_non_200_response():
    status = providers.ollama_status(http_client=_Client(_Response(status_code=500)))
    assert status.available is False


def test_ollama_unavailable_when_the_body_is_not_json():
    status = providers.ollama_status(http_client=_Client(_Response(raises=True)))
    assert status.available is False


def test_ollama_available_with_real_models_from_the_instance():
    payload = {"models": [{"name": "llama3:latest"}, {"name": "mistral:7b"}]}
    status = providers.ollama_status(http_client=_Client(_Response(payload=payload)))

    assert status.available is True
    assert status.models == ["llama3:latest", "mistral:7b"]
    assert status.kind == "local"


def test_ollama_running_with_no_models_is_not_reported_available():
    """Running but empty is a real, distinct state: the user must pull a
    model themselves. Reporting it as available would promise something
    that would then fail at first use."""
    status = providers.ollama_status(http_client=_Client(_Response(payload={"models": []})))

    assert status.available is False
    assert "no models installed" in status.detail


@pytest.mark.parametrize("payload", [
    None, [], "models", {"models": "llama3"}, {"models": [None, 5]},
    {"models": [{"nome": "typo"}]}, {"models": [{"name": ""}]},
])
def test_malformed_payloads_never_produce_invented_models(payload):
    status = providers.ollama_status(http_client=_Client(_Response(payload=payload)))
    assert status.models == []
    assert status.available is False


def test_ollama_probe_targets_loopback_with_a_short_timeout():
    """Loopback only, and fast: an unreachable local server must not
    stall onboarding."""
    client = _Client(_Response(payload={"models": [{"name": "x"}]}))
    providers.ollama_status(http_client=client)

    call = client.calls[0]
    assert call["url"].startswith("http://127.0.0.1:11434")
    assert call["timeout"] <= 2


def test_ollama_detection_never_triggers_a_model_download():
    """Only /api/tags (a read) is ever called — never /api/pull."""
    client = _Client(_Response(payload={"models": [{"name": "llama3"}]}))
    providers.ollama_status(http_client=client)

    assert all("/api/tags" in c["url"] for c in client.calls)
    assert not any("pull" in c["url"] for c in client.calls)


# ---------------------------------------------------------------------------
# Aggregate + selection
# ---------------------------------------------------------------------------

def test_detect_all_reports_both_known_providers():
    names = {s.name for s in providers.detect_all(http_client=_Client(error=OSError()))}
    assert names == set(providers.KNOWN_PROVIDERS)


def test_is_valid_provider():
    assert providers.is_valid_provider("anthropic") is True
    assert providers.is_valid_provider("ollama") is True
    assert providers.is_valid_provider("openai") is False
    assert providers.is_valid_provider("") is False


def test_selected_provider_normalises_and_falls_back(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "jarvis_ai_provider", "  OLLAMA ")
    assert providers.selected_provider() == "ollama"

    monkeypatch.setattr(settings, "jarvis_ai_provider", "not-a-provider")
    assert providers.selected_provider() == "anthropic"
