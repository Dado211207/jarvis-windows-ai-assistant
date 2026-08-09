"""Tests for the chat pipeline: honest failure messages, conversation
history, streaming, stop-generation and reset.

The headline property is the one the previous implementation got wrong:
**the message a user reads must match the cause**. Every failure used to
produce "AI responses aren't set up yet — add an API key", including for
people whose key was fine and who were being rate-limited. Several tests
here exist purely to keep those cases distinguishable.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.ai.base import Message, ProviderError
from app.core.errors import ErrorCategory
from tests.conftest import prime_session


@pytest.fixture
def client():
    from app.api.server import app
    with TestClient(app) as test_client:
        yield prime_session(test_client)


@pytest.fixture(autouse=True)
def _privacy_off():
    from app.core.privacy import privacy_mode
    privacy_mode.set(False)
    yield
    privacy_mode.set(False)


def _brain_with_key():
    """A Brain whose settings say a key is configured. The provider
    itself is mocked separately per test."""
    from app.core.brain import Brain
    return Brain()


def _settings_patch(stack_settings, provider="anthropic"):
    stack_settings.has_anthropic_key = True
    stack_settings.effective_api_key = "sk-test-key"
    stack_settings.jarvis_ai_provider = provider
    stack_settings.jarvis_ai_model = "claude-haiku-4-5-20251001"
    stack_settings.jarvis_ai_max_tokens = 250
    stack_settings.jarvis_ai_timeout_seconds = 20
    stack_settings.jarvis_ollama_model = ""


# ---------------------------------------------------------------------------
# Honest failure messages — the reason this pipeline was rewritten
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("category,must_not_say", [
    (ErrorCategory.PROVIDER_RATE_LIMIT, "add an"),
    (ErrorCategory.PROVIDER_TIMEOUT, "add an"),
    (ErrorCategory.PROVIDER_UNAVAILABLE, "add an"),
])
def test_a_failure_that_is_not_a_missing_key_never_tells_you_to_add_a_key(category, must_not_say):
    brain = _brain_with_key()

    with patch("app.core.brain.settings") as settings:
        _settings_patch(settings)
        with patch.object(brain, "provider") as get:
            get.return_value.name = "anthropic"
            get.return_value.resolved_model.return_value = "claude-haiku-4-5-20251001"
            get.return_value.availability.return_value = MagicMock(ready=True, reason="")
            get.return_value.generate.side_effect = ProviderError(category)
            result = brain.generate_response("hello")

    assert must_not_say not in result.content.lower()
    assert result.error.category == category


def test_an_expired_key_says_the_key_was_rejected():
    brain = _brain_with_key()

    with patch("app.core.brain.settings") as settings:
        _settings_patch(settings)
        with patch.object(brain, "provider") as get:
            get.return_value.name = "anthropic"
            get.return_value.resolved_model.return_value = "m"
            get.return_value.availability.return_value = MagicMock(ready=True, reason="")
            get.return_value.generate.side_effect = ProviderError(ErrorCategory.PROVIDER_AUTH)
            result = brain.generate_response("hello")

    assert "rejected" in result.content.lower()
    assert "settings" in result.content.lower()  # still tells them where to fix it


def test_a_rate_limit_says_to_try_again_shortly():
    brain = _brain_with_key()

    with patch("app.core.brain.settings") as settings:
        _settings_patch(settings)
        with patch.object(brain, "provider") as get:
            get.return_value.name = "anthropic"
            get.return_value.resolved_model.return_value = "m"
            get.return_value.availability.return_value = MagicMock(ready=True, reason="")
            get.return_value.generate.side_effect = ProviderError(ErrorCategory.PROVIDER_RATE_LIMIT)
            result = brain.generate_response("hello")

    assert "rate-limit" in result.content.lower()


def test_a_provider_authored_detail_is_preferred_over_the_generic_message():
    """When the provider knows something specific and credential-free —
    which local models are installed — that beats the generic sentence."""
    brain = _brain_with_key()
    detail = "Ollama is running but the selected model 'mistral' is not installed."

    with patch("app.core.brain.settings") as settings:
        _settings_patch(settings, provider="ollama")
        with patch.object(brain, "provider") as get:
            get.return_value.name = "ollama"
            get.return_value.resolved_model.return_value = "mistral"
            get.return_value.availability.return_value = MagicMock(ready=True, reason="")
            get.return_value.generate.side_effect = ProviderError(
                ErrorCategory.PROVIDER_UNAVAILABLE, detail=detail
            )
            result = brain.generate_response("hello")

    assert result.content == detail


def test_an_unconfigured_provider_is_not_reported_as_an_error():
    """A fresh install is the normal state, not a fault: no correlation
    ID, nothing logged as a failure."""
    brain = _brain_with_key()

    with patch("app.core.brain.settings") as settings:
        _settings_patch(settings)
        settings.has_anthropic_key = False
        result = brain.generate_response("hello")

    assert result.error is None
    assert result.provider == "local"


def test_a_provider_that_breaks_its_own_contract_still_produces_a_safe_error():
    """A raw exception escaping a provider must not reach the user."""
    brain = _brain_with_key()

    with patch("app.core.brain.settings") as settings:
        _settings_patch(settings)
        with patch.object(brain, "provider") as get:
            get.return_value.name = "anthropic"
            get.return_value.resolved_model.return_value = "m"
            get.return_value.availability.return_value = MagicMock(ready=True, reason="")
            get.return_value.generate.side_effect = RuntimeError("token=sk-leak /home/me/secrets")
            result = brain.generate_response("hello")

    assert "sk-leak" not in result.content
    assert "sk-leak" not in result.error.model_dump_json()
    assert result.error.correlation_id


def test_provider_ready_never_raises_even_when_detection_explodes():
    brain = _brain_with_key()

    with patch("app.core.brain.settings") as settings:
        _settings_patch(settings)
        with patch.object(brain, "provider", side_effect=OSError("detection blew up")):
            ready, reason = brain.provider_ready()

    assert ready is False
    assert "still work" in reason


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

def test_history_is_replayed_so_a_follow_up_question_makes_sense():
    from app.core import conversation

    stored = [
        MagicMock(role="assistant", content="Paris."),
        MagicMock(role="user", content="capital of France?"),
    ]
    db = MagicMock()
    db.get_recent_conversations.return_value = stored

    with patch("db.database.get_db", return_value=db):
        messages = conversation.build_request_messages("and of Spain?")

    assert [m.content for m in messages] == ["capital of France?", "Paris.", "and of Spain?"]
    assert messages[0].role == "user"
    assert messages[1].role == "assistant"


def test_history_is_bounded():
    from app.core import conversation

    db = MagicMock()
    db.get_recent_conversations.return_value = []
    with patch("db.database.get_db", return_value=db):
        conversation.recent_messages()

    assert db.get_recent_conversations.call_args.kwargs["limit"] == conversation.MAX_HISTORY_TURNS
    assert conversation.MAX_HISTORY_TURNS <= 20, "an unbounded transcript would grow every request"


def test_privacy_mode_suppresses_history_entirely():
    """Reading stored turns back out and sending them to a cloud provider
    would drive straight through privacy mode's own guarantee."""
    from app.core import conversation
    from app.core.privacy import privacy_mode

    db = MagicMock()
    db.get_recent_conversations.return_value = [MagicMock(role="user", content="private thing")]

    privacy_mode.set(True)
    with patch("db.database.get_db", return_value=db):
        messages = conversation.build_request_messages("now what?")

    assert [m.content for m in messages] == ["now what?"]
    db.get_recent_conversations.assert_not_called()


def test_privacy_mode_suppresses_persistence():
    from app.core import conversation
    from app.core.privacy import privacy_mode

    db = MagicMock()
    privacy_mode.set(True)
    with patch("db.database.get_db", return_value=db):
        assert conversation.record_exchange("q", "a") is False

    db.add_conversation.assert_not_called()


def test_a_broken_database_degrades_history_rather_than_breaking_chat():
    from app.core import conversation

    with patch("db.database.get_db", side_effect=OSError("database is locked")):
        assert conversation.recent_messages() == []
        assert conversation.record_exchange("q", "a") is False


def test_malformed_history_rows_are_skipped_not_sent():
    from app.core import conversation

    db = MagicMock()
    db.get_recent_conversations.return_value = [
        MagicMock(role="system", content="not a valid chat role"),
        MagicMock(role="user", content=""),
        MagicMock(role="user", content=None),
        MagicMock(role="user", content="a real one"),
    ]

    with patch("db.database.get_db", return_value=db):
        messages = conversation.recent_messages()

    assert [m.content for m in messages] == ["a real one"]


def test_reset_does_not_touch_the_action_audit_trail():
    from app.core import conversation

    db = MagicMock()
    db.clear_conversations.return_value = 4
    with patch("db.database.get_db", return_value=db):
        assert conversation.reset() == 4

    db.clear_conversations.assert_called_once()
    db.clear_logs.assert_not_called()


# ---------------------------------------------------------------------------
# Streaming endpoint
# ---------------------------------------------------------------------------

def _events(response):
    parsed = []
    for frame in response.text.split("\n\n"):
        frame = frame.strip()
        if frame.startswith("data:"):
            parsed.append(json.loads(frame[5:].strip()))
    return parsed


def test_stream_requires_the_session_token():
    from app.api.server import app

    with TestClient(app) as bare:  # deliberately not primed
        bare.get("/health")
        bare.cookies.clear()
        assert bare.post("/chat/stream", json={"command": "hi"}).status_code == 403


def test_a_deterministic_command_is_executed_not_streamed(client):
    """Routes still take priority — the AI never sees "status", and the
    result comes back through the ordinary policy-gated path."""
    events = _events(client.post("/chat/stream", json={"command": "status"}))

    assert [e["type"] for e in events] == ["routed", "done"]
    assert events[0]["response"]["tool_used"] == "status"


def test_an_approval_required_command_streams_its_pending_action(client):
    """The approval gate is not bypassed by using this endpoint."""
    events = _events(client.post("/chat/stream", json={"command": "read clipboard"}))

    routed = events[0]["response"]
    assert routed["requires_approval"] is True
    assert routed["pending_action_id"]


def test_an_empty_command_is_reported_and_still_terminates(client):
    events = _events(client.post("/chat/stream", json={"command": "   "}))

    assert events[0]["type"] == "error"
    assert events[-1]["type"] == "done"


def test_no_provider_configured_streams_an_explanation_not_a_failure(client):
    with patch("app.core.brain.settings") as settings:
        _settings_patch(settings)
        settings.has_anthropic_key = False
        events = _events(client.post("/chat/stream", json={"command": "tell me a joke"}))

    assert [e["type"] for e in events] == ["start", "delta", "done"]
    assert "Settings" in events[1]["text"]
    assert events[-1]["used_api"] is False


def test_a_successful_generation_streams_deltas_then_done(client):
    from app.core.brain import brain

    provider = MagicMock()
    provider.name = "anthropic"
    provider.resolved_model.return_value = "claude-haiku-4-5-20251001"
    provider.availability.return_value = MagicMock(ready=True, reason="")
    provider.stream.return_value = iter(["Hel", "lo"])

    with patch.object(brain, "provider", return_value=provider):
        events = _events(client.post("/chat/stream", json={"command": "say hello"}))

    assert [e["type"] for e in events] == ["start", "delta", "delta", "done"]
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "Hello"
    assert events[-1]["stopped"] is False
    assert events[-1]["persisted"] is True


def test_a_failing_generation_reports_the_error_and_still_sends_done(client):
    """A client waiting for `done` must never hang, whatever went wrong."""
    from app.core.brain import brain

    provider = MagicMock()
    provider.name = "anthropic"
    provider.resolved_model.return_value = "m"
    provider.availability.return_value = MagicMock(ready=True, reason="")
    provider.stream.side_effect = ProviderError(ErrorCategory.PROVIDER_RATE_LIMIT)

    with patch.object(brain, "provider", return_value=provider):
        events = _events(client.post("/chat/stream", json={"command": "say hello"}))

    assert [e["type"] for e in events] == ["start", "error", "done"]
    assert events[1]["error"]["category"] == "provider_rate_limit"
    assert events[-1]["used_api"] is False
    assert events[-1]["persisted"] is False, "there is no answer worth remembering"


def test_a_streaming_error_never_carries_raw_provider_text(client):
    from app.core.brain import brain

    provider = MagicMock()
    provider.name = "anthropic"
    provider.resolved_model.return_value = "m"
    provider.availability.return_value = MagicMock(ready=True, reason="")
    provider.stream.side_effect = RuntimeError("Bearer sk-ant-LEAKED at /home/me/.jarvis")

    with patch.object(brain, "provider", return_value=provider):
        raw = client.post("/chat/stream", json={"command": "hi"}).text

    assert "sk-ant-LEAKED" not in raw
    assert "/home/me/.jarvis" not in raw


def test_a_stopped_generation_keeps_what_the_user_already_saw(client):
    """The words were on screen; a follow-up question about them has to
    still make sense."""
    from app.core.brain import brain
    from app.core.generation import generations

    def _stream(command, cancel=None):
        yield "first "
        generations.stop_all()   # exactly what the Stop button does
        yield "second "
        yield "third"

    provider = MagicMock()
    provider.name = "anthropic"
    provider.resolved_model.return_value = "m"
    provider.availability.return_value = MagicMock(ready=True, reason="")

    with patch.object(brain, "provider", return_value=provider), \
         patch.object(brain, "stream_response", side_effect=_stream):
        events = _events(client.post("/chat/stream", json={"command": "count"}))

    deltas = [e["text"] for e in events if e["type"] == "delta"]
    assert deltas == ["first "], "not one chunk past the stop"
    assert events[-1]["stopped"] is True
    assert events[-1]["persisted"] is True


def test_the_generation_registry_is_emptied_when_a_stream_ends(client):
    from app.core.brain import brain
    from app.core.generation import generations

    provider = MagicMock()
    provider.name = "anthropic"
    provider.resolved_model.return_value = "m"
    provider.availability.return_value = MagicMock(ready=True, reason="")
    provider.stream.return_value = iter(["done"])

    with patch.object(brain, "provider", return_value=provider):
        client.post("/chat/stream", json={"command": "hi"})

    assert generations.active_count() == 0


def test_the_runtime_returns_to_standby_after_a_failed_stream(client):
    from app.core.brain import brain
    from app.core.runtime_state import RuntimeState, runtime

    provider = MagicMock()
    provider.name = "anthropic"
    provider.resolved_model.return_value = "m"
    provider.availability.return_value = MagicMock(ready=True, reason="")
    provider.stream.side_effect = ProviderError(ErrorCategory.PROVIDER_ERROR)

    with patch.object(brain, "provider", return_value=provider):
        client.post("/chat/stream", json={"command": "hi"})

    assert runtime.state == RuntimeState.STANDBY


def test_privacy_mode_stops_a_streamed_answer_being_persisted(client):
    from app.core.brain import brain
    from app.core.privacy import privacy_mode

    provider = MagicMock()
    provider.name = "anthropic"
    provider.resolved_model.return_value = "m"
    provider.availability.return_value = MagicMock(ready=True, reason="")
    provider.stream.return_value = iter(["private answer"])

    privacy_mode.set(True)
    with patch.object(brain, "provider", return_value=provider):
        events = _events(client.post("/chat/stream", json={"command": "hi"}))

    assert events[-1]["persisted"] is False


def test_stream_response_is_not_broadcast_to_every_websocket_client(client):
    """Chat content belongs to the request that asked for it. The event
    bus is a broadcast to all connected clients, so no chat text may
    reach it."""
    from app.core.brain import brain
    from app.core.events import event_bus

    provider = MagicMock()
    provider.name = "anthropic"
    provider.resolved_model.return_value = "m"
    provider.availability.return_value = MagicMock(ready=True, reason="")
    provider.stream.return_value = iter(["a secret answer"])

    last = event_bus.latest_seq()
    with patch.object(brain, "provider", return_value=provider):
        client.post("/chat/stream", json={"command": "hi"})

    published = json.dumps([e.payload for e in event_bus.since(last)])
    assert "a secret answer" not in published


# ---------------------------------------------------------------------------
# Stop and reset endpoints
# ---------------------------------------------------------------------------

def test_stopping_something_that_already_finished_is_not_an_error(client):
    body = client.post("/chat/stop", json={"generation_id": "long-gone"}).json()

    assert body["stopped"] is False
    assert "finished" in body["message"]


def test_stop_without_an_id_stops_whatever_is_running(client):
    from app.core.generation import generations

    generation_id, token = generations.start()
    try:
        body = client.post("/chat/stop", json={}).json()
        assert body["count"] == 1
        assert token.cancelled is True
    finally:
        generations.finish(generation_id)


def test_stop_requires_the_session_token():
    from app.api.server import app

    with TestClient(app) as bare:
        bare.get("/health")
        bare.cookies.clear()
        assert bare.post("/chat/stop", json={}).status_code == 403


def test_reset_clears_history_and_reports_how_much(client):
    db = MagicMock()
    db.clear_conversations.return_value = 7

    with patch("db.database.get_db", return_value=db):
        body = client.post("/conversation/reset", json={}).json()

    assert body["success"] is True
    assert body["removed"] == 7


def test_reset_with_nothing_stored_says_so(client):
    db = MagicMock()
    db.clear_conversations.return_value = 0

    with patch("db.database.get_db", return_value=db):
        body = client.post("/conversation/reset", json={}).json()

    assert "no chat history" in body["message"].lower()


def test_reset_failure_is_reported_safely(client):
    with patch("db.database.get_db", side_effect=OSError("/home/me/data/jarvis.db is locked")):
        response = client.post("/conversation/reset", json={})

    assert response.json()["success"] is False
    assert "/home/me/data" not in response.text


def test_reset_requires_the_session_token():
    from app.api.server import app

    with TestClient(app) as bare:
        bare.get("/health")
        bare.cookies.clear()
        assert bare.post("/conversation/reset", json={}).status_code == 403


# ---------------------------------------------------------------------------
# The app stays usable with no AI at all
# ---------------------------------------------------------------------------

def test_local_commands_work_with_no_provider_configured(client):
    with patch("app.core.brain.settings") as settings:
        _settings_patch(settings)
        settings.has_anthropic_key = False
        response = client.post("/command", json={"command": "status"})

    assert response.json()["success"] is True
    assert response.json()["tool_used"] == "status"


# ---------------------------------------------------------------------------
# Chat page markup and client rules
# ---------------------------------------------------------------------------

def _chat_js() -> str:
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    return js[js.index("// ── Chat"):js.index("// ── Push-to-talk")]


@pytest.mark.parametrize("element_id", ["chat-stop", "chat-reset", "chat-provider", "chat-status"])
def test_chat_page_has_the_new_controls(client, element_id):
    assert f'id="{element_id}"' in client.get("/ui/chat").text


def test_the_stop_button_starts_hidden(client):
    """It only means something while something is generating."""
    body = client.get("/ui/chat").text
    stop = body[body.index('id="chat-stop"'):]
    assert "hidden" in stop[:400]


def test_chat_status_is_a_live_region(client):
    """"Stopped." and error references appear there while the user is
    reading the answer, not the status line."""
    body = client.get("/ui/chat").text
    index = body.index('id="chat-status"')
    assert "aria-live" in body[index:index + 200]


def test_streaming_client_never_builds_markup_from_a_response():
    js = _chat_js()
    assert "innerHTML" not in js
    assert "insertAdjacentHTML" not in js


def test_streaming_falls_back_to_the_plain_endpoint():
    """A browser that cannot stream a response body must still be able to
    chat, so the failure path is a fallback, not an error message."""
    js = _chat_js()
    assert "sendChatFallback" in js
    assert '"/command"' in js


def test_reset_asks_before_deleting():
    js = _chat_js()
    assert "window.confirm" in js
    assert "cannot be undone" in js


def test_reset_tells_the_user_logs_are_not_affected():
    assert "logs are not affected" in _chat_js()
