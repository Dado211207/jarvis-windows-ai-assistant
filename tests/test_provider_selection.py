"""Tests for choosing an AI provider from Settings.

This exists because a provider that can only be selected by setting an
environment variable is not selectable at all for someone running the
installed JARVIS.exe. The properties under test are that the choice
sticks, that it can never claim something undetected, and that the
preferences file stays what it is — a two-key allowlist, never a
credential store and never a browser-writable settings backdoor.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import prime_session


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Every test gets its own config dir, so nothing writes into the
    developer's real preferences and no test can depend on another's."""
    monkeypatch.setattr("app.core.app_paths.config_dir", lambda: tmp_path)
    monkeypatch.setattr("app.core.preferences.config_dir", lambda: tmp_path)
    yield tmp_path


@pytest.fixture
def client():
    from app.api.server import app
    with TestClient(app) as test_client:
        yield prime_session(test_client)


def _status(name, display, kind, available, models=()):
    from app.core.providers import ProviderStatus

    return ProviderStatus(
        name=name, display_name=display, kind=kind, available=available,
        detail=f"{display} is {'available' if available else 'not detected'}.",
        models=list(models),
    )


# ---------------------------------------------------------------------------
# The preferences file itself
# ---------------------------------------------------------------------------

def test_a_saved_preference_round_trips():
    from app.core import preferences

    assert preferences.store("ai_provider", "ollama") is True
    assert preferences.get("ai_provider") == "ollama"


def test_nothing_saved_reads_as_nothing():
    from app.core import preferences

    assert preferences.load() == {}
    assert preferences.get("ai_provider") is None


def test_a_key_outside_the_allowlist_is_refused():
    """The allowlist is the whole safety story: this must never become a
    general "write any setting from the browser" mechanism."""
    from app.core import preferences

    assert preferences.store("anthropic_api_key", "sk-should-never-be-stored") is False
    assert preferences.load() == {}


def test_a_credential_can_never_be_read_back_out_of_preferences(isolated_config):
    """Even if something wrote one into the file directly, load() only
    ever returns allowlisted keys."""
    from app.core import preferences

    (isolated_config / preferences.PREFERENCES_FILENAME).write_text(
        '{"ai_provider": "ollama", "anthropic_api_key": "sk-planted-secret"}', encoding="utf-8"
    )

    assert preferences.load() == {"ai_provider": "ollama"}
    assert preferences.get("anthropic_api_key") is None


def test_a_corrupt_file_degrades_to_defaults_rather_than_crashing(isolated_config):
    from app.core import preferences

    (isolated_config / preferences.PREFERENCES_FILENAME).write_text("{not json", encoding="utf-8")

    assert preferences.load() == {}


@pytest.mark.parametrize("written", ["[]", '"a string"', "null"])
def test_a_file_of_the_wrong_shape_is_ignored(isolated_config, written):
    from app.core import preferences

    (isolated_config / preferences.PREFERENCES_FILENAME).write_text(written, encoding="utf-8")

    assert preferences.load() == {}


def test_clearing_a_preference_removes_it():
    from app.core import preferences

    preferences.store("ollama_model", "llama3:latest")
    preferences.store("ollama_model", "")

    assert preferences.get("ollama_model") is None


def test_an_unwritable_config_directory_is_reported_not_raised(monkeypatch):
    from app.core import preferences

    monkeypatch.setattr(preferences, "config_dir", lambda: (_ for _ in ()).throw(OSError("read-only")))
    assert preferences.store("ai_provider", "ollama") is False


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------

def test_a_saved_choice_wins_over_the_configured_default(monkeypatch):
    """The env var supplies the starting default; a choice made later in
    the app holds. The other precedence would give a picker that silently
    does nothing on a machine where the variable happens to be set."""
    from app.config import settings
    from app.core import preferences
    from app.core.providers import selected_provider

    monkeypatch.setattr(settings, "jarvis_ai_provider", "anthropic")
    preferences.store("ai_provider", "ollama")

    assert selected_provider() == "ollama"


def test_the_configured_default_applies_when_nothing_is_saved(monkeypatch):
    from app.config import settings
    from app.core.providers import selected_provider

    monkeypatch.setattr(settings, "jarvis_ai_provider", "ollama")

    assert selected_provider() == "ollama"


def test_a_saved_nonsense_provider_still_normalises(monkeypatch):
    from app.core import preferences
    from app.core.providers import selected_provider

    preferences.store("ai_provider", "not-a-real-provider")

    assert selected_provider() == "anthropic"


def test_the_brain_uses_the_saved_provider():
    from app.core import preferences
    from app.core.brain import Brain

    preferences.store("ai_provider", "ollama")

    assert Brain().provider_name() == "ollama"
    assert Brain().provider().name == "ollama"


def test_the_brain_passes_the_saved_ollama_model_to_the_provider():
    from app.core import preferences
    from app.core.brain import Brain

    preferences.store("ai_provider", "ollama")
    preferences.store("ollama_model", "llama3:latest")

    assert Brain().provider().config.ollama_model == "llama3:latest"


# ---------------------------------------------------------------------------
# The selection endpoint
# ---------------------------------------------------------------------------

def test_selecting_an_available_provider_sticks(client):
    detected = [
        _status("anthropic", "Anthropic (Claude)", "cloud", False),
        _status("ollama", "Ollama (local models)", "local", True, ["llama3:latest"]),
    ]

    with patch("app.core.providers.detect_all", return_value=detected), \
         patch("app.core.providers.ollama_status", return_value=detected[1]), \
         patch("app.core.providers.anthropic_status", return_value=detected[0]):
        response = client.post("/providers/select", json={"provider": "ollama", "model": "llama3:latest"})

    assert response.status_code == 200
    from app.core.preferences import get as get_preference
    assert get_preference("ai_provider") == "ollama"
    assert get_preference("ollama_model") == "llama3:latest"


def test_a_provider_that_is_not_detected_cannot_be_selected(client):
    """The rule the whole app follows: never claim a capability that was
    not actually detected."""
    detected = [
        _status("anthropic", "Anthropic (Claude)", "cloud", True),
        _status("ollama", "Ollama (local models)", "local", False),
    ]

    with patch("app.core.providers.detect_all", return_value=detected):
        response = client.post("/providers/select", json={"provider": "ollama"})

    assert response.status_code == 409
    assert "not detected" in response.json()["detail"]

    from app.core.preferences import get as get_preference
    assert get_preference("ai_provider") is None


def test_a_model_the_instance_does_not_report_is_refused_and_named(client):
    detected = [
        _status("anthropic", "Anthropic (Claude)", "cloud", False),
        _status("ollama", "Ollama (local models)", "local", True, ["llama3:latest"]),
    ]

    with patch("app.core.providers.detect_all", return_value=detected):
        response = client.post("/providers/select", json={"provider": "ollama", "model": "mistral"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "mistral" in detail
    assert "llama3:latest" in detail, "the refusal must say what IS installed"


def test_an_unknown_provider_name_is_rejected(client):
    assert client.post("/providers/select", json={"provider": "openai"}).status_code == 400


def test_selecting_a_provider_requires_the_session_token():
    from app.api.server import app

    with TestClient(app) as bare:
        bare.get("/health")
        bare.cookies.clear()
        assert bare.post("/providers/select", json={"provider": "anthropic"}).status_code == 403


def test_the_providers_response_never_contains_a_credential(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(type(settings), "effective_api_key", property(lambda self: "sk-provider-list-leak"))
    monkeypatch.setattr(type(settings), "has_anthropic_key", property(lambda self: True))

    raw = client.get("/providers").text

    assert "sk-" not in raw


def test_the_providers_response_reports_the_model_in_effect(client):
    detected = [
        _status("anthropic", "Anthropic (Claude)", "cloud", False),
        _status("ollama", "Ollama (local models)", "local", True, ["llama3:latest"]),
    ]
    from app.core import preferences
    preferences.store("ai_provider", "ollama")
    preferences.store("ollama_model", "llama3:latest")

    with patch("app.core.providers.detect_all", return_value=detected):
        body = client.get("/providers").json()

    assert body["selected"] == "ollama"
    assert body["selected_model"] == "llama3:latest"


# ---------------------------------------------------------------------------
# The Settings page
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("element_id", [
    "settings-provider-select", "settings-ollama-model", "settings-provider-save",
    "settings-provider-message",
])
def test_the_settings_page_offers_the_picker(client, element_id):
    assert f'id="{element_id}"' in client.get("/ui/settings").text


def test_the_settings_page_states_that_models_are_never_downloaded(client):
    assert "never downloads" in client.get("/ui/settings").text


def test_the_picker_javascript_disables_undetected_providers():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    picker = js[js.index("function populateProviderPicker"):js.index("async function saveProviderSelection")]

    assert "option.disabled = !provider.available" in picker
    assert "not detected" in picker, "an undetected provider is shown as such, not hidden"
    assert "innerHTML" not in picker
