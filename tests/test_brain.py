"""Tests for Phase 2: Claude AI Brain integration.

All tests mock the Anthropic SDK — no real API calls are made.
"""

import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_brain(api_key: str = ""):
    """Return a fresh Brain instance with the given API key patched into settings."""
    from app.core.brain import Brain
    with patch("app.core.brain.settings") as mock_settings:
        mock_settings.has_anthropic_key = bool(api_key.strip())
        mock_settings.anthropic_api_key = api_key
        mock_settings.jarvis_ai_provider = "anthropic"
        mock_settings.jarvis_ai_model = "claude-haiku-4-5-20251001"
        mock_settings.jarvis_ai_max_tokens = 250
        mock_settings.jarvis_ai_timeout_seconds = 20
        mock_settings.jarvis_db_path = "data/jarvis.db"
        mock_settings.jarvis_log_file = "data/logs/jarvis.log"
        b = Brain()
    return b


# ---------------------------------------------------------------------------
# 1. is_configured()
# ---------------------------------------------------------------------------

def test_is_configured_false_when_no_key():
    from app.core.brain import Brain
    b = Brain()
    with patch("app.core.brain.settings") as s:
        s.has_anthropic_key = False
        assert b.is_configured() is False


def test_is_configured_true_when_key_present():
    from app.core.brain import Brain
    b = Brain()
    with patch("app.core.brain.settings") as s:
        s.has_anthropic_key = True
        assert b.is_configured() is True


# ---------------------------------------------------------------------------
# 2. generate_response() — local fallback (no key)
# ---------------------------------------------------------------------------

def test_generate_response_local_fallback_when_no_key():
    from app.core.brain import Brain
    b = Brain()
    with patch("app.core.brain.settings") as s:
        s.has_anthropic_key = False
        s.jarvis_ai_provider = "anthropic"
        result = b.generate_response("what is the weather?")

    assert result.used_api is False
    assert result.provider == "local"
    assert "Claude AI is not configured" in result.content
    assert result.error is None


# ---------------------------------------------------------------------------
# 3. generate_response() — mocked Anthropic success
# ---------------------------------------------------------------------------

def _mock_anthropic_message(text: str):
    """Build a minimal mock of an Anthropic Messages response."""
    content_block = MagicMock()
    content_block.text = text
    msg = MagicMock()
    msg.content = [content_block]
    msg.usage = MagicMock()
    return msg


def test_generate_response_uses_api_when_key_present():
    from app.core.brain import Brain
    b = Brain()

    fake_reply = _mock_anthropic_message("The weather is sunny today.")

    with patch("app.core.brain.settings") as s, \
         patch("anthropic.Anthropic") as mock_anthropic_cls:
        s.has_anthropic_key = True
        s.anthropic_api_key = "sk-test-key"
        s.jarvis_ai_provider = "anthropic"
        s.jarvis_ai_model = "claude-haiku-4-5-20251001"
        s.jarvis_ai_max_tokens = 250
        s.jarvis_ai_timeout_seconds = 20

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = fake_reply

        result = b.generate_response("what is the weather?")

    assert result.used_api is True
    assert result.provider == "anthropic"
    assert result.content == "The weather is sunny today."
    assert result.error is None
    mock_client.messages.create.assert_called_once()


def test_generate_response_passes_system_prompt():
    """Verify the system prompt is forwarded to the Anthropic SDK."""
    from app.core.brain import Brain
    from app.core.system_prompt import SYSTEM_PROMPT
    b = Brain()

    fake_reply = _mock_anthropic_message("I can help with that.")

    with patch("app.core.brain.settings") as s, \
         patch("anthropic.Anthropic") as mock_anthropic_cls:
        s.has_anthropic_key = True
        s.anthropic_api_key = "sk-test-key"
        s.jarvis_ai_provider = "anthropic"
        s.jarvis_ai_model = "claude-haiku-4-5-20251001"
        s.jarvis_ai_max_tokens = 250
        s.jarvis_ai_timeout_seconds = 20

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = fake_reply

        b.generate_response("tell me a joke")

    call_kwargs = mock_client.messages.create.call_args
    assert call_kwargs.kwargs.get("system") == SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 4. generate_response() — API error falls back to local message
# ---------------------------------------------------------------------------

def test_generate_response_falls_back_on_api_error():
    from app.core.brain import Brain
    b = Brain()

    with patch("app.core.brain.settings") as s, \
         patch("anthropic.Anthropic") as mock_anthropic_cls:
        s.has_anthropic_key = True
        s.anthropic_api_key = "sk-bad-key"
        s.jarvis_ai_provider = "anthropic"
        s.jarvis_ai_model = "claude-haiku-4-5-20251001"
        s.jarvis_ai_max_tokens = 250
        s.jarvis_ai_timeout_seconds = 20

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = Exception("401 Unauthorized")

        result = b.generate_response("hello")

    assert result.used_api is False
    assert result.error is not None
    assert "401" in result.error


def test_generate_response_falls_back_on_timeout():
    from app.core.brain import Brain
    import socket
    b = Brain()

    with patch("app.core.brain.settings") as s, \
         patch("anthropic.Anthropic") as mock_anthropic_cls:
        s.has_anthropic_key = True
        s.anthropic_api_key = "sk-test-key"
        s.jarvis_ai_provider = "anthropic"
        s.jarvis_ai_model = "claude-haiku-4-5-20251001"
        s.jarvis_ai_max_tokens = 250
        s.jarvis_ai_timeout_seconds = 20

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = TimeoutError("Request timed out")

        result = b.generate_response("slow question")

    assert result.used_api is False
    assert result.error is not None


# ---------------------------------------------------------------------------
# 5. Router: known commands use tools, not brain
# ---------------------------------------------------------------------------

def test_known_command_does_not_invoke_brain():
    """'system status' should be handled by the tool registry, not the brain."""
    from app.core.brain import Brain
    from app.core.router import CommandRouter
    from app.core.tool_registry import ToolRegistry
    from app.core.models import PermissionLevel, ToolCategory, ToolDefinition

    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name="system_status",
            description="test",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.SYSTEM,
        ),
        lambda: {"success": True, "message": "cpu ok", "data": None},
    )

    mock_brain = MagicMock(spec=Brain)

    with patch("db.database.get_db") as mock_db:
        mock_db.return_value.log_action = MagicMock()
        router = CommandRouter(reg, brain=mock_brain)
        resp = router.route("system status")

    mock_brain.generate_response.assert_not_called()
    assert resp.success is True
    assert resp.tool_used == "system_status"


# ---------------------------------------------------------------------------
# 6. Router: unknown commands delegate to brain
# ---------------------------------------------------------------------------

def test_unknown_command_delegates_to_brain():
    """A command with no route match should be forwarded to brain."""
    from app.core.brain import Brain
    from app.core.router import CommandRouter
    from app.core.tool_registry import ToolRegistry
    from app.core.models import BrainResponse

    mock_brain = MagicMock(spec=Brain)
    mock_brain.generate_response.return_value = BrainResponse(
        content="I can help with that.",
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        used_api=True,
    )

    with patch("db.database.get_db") as mock_db:
        mock_db.return_value.add_conversation = MagicMock()
        router = CommandRouter(ToolRegistry(), brain=mock_brain)
        resp = router.route("what is the capital of France?")

    mock_brain.generate_response.assert_called_once_with("what is the capital of France?")
    assert resp.success is True
    assert resp.tool_used == "brain"
    assert "I can help with that." in resp.message


def test_unknown_command_without_brain_returns_failure():
    """Without a brain, unknown commands return success=False."""
    from app.core.router import CommandRouter
    from app.core.tool_registry import ToolRegistry

    router = CommandRouter(ToolRegistry(), brain=None)
    resp = router.route("do something weird")
    assert resp.success is False
    assert "Unknown command" in resp.message


# ---------------------------------------------------------------------------
# 7. Conversation stored in DB after brain response
# ---------------------------------------------------------------------------

def test_brain_response_stored_in_db():
    from app.core.brain import Brain
    from app.core.router import CommandRouter
    from app.core.tool_registry import ToolRegistry
    from app.core.models import BrainResponse

    mock_brain = MagicMock(spec=Brain)
    mock_brain.generate_response.return_value = BrainResponse(
        content="Paris.",
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        used_api=True,
    )

    mock_db = MagicMock()
    with patch("db.database.get_db", return_value=mock_db):
        router = CommandRouter(ToolRegistry(), brain=mock_brain)
        router.route("capital of France?")

    calls = [c.args for c in mock_db.add_conversation.call_args_list]
    assert ("user", "capital of France?") in calls
    assert ("assistant", "Paris.") in calls


# ---------------------------------------------------------------------------
# 8. API /command with AI response
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield client


def test_api_command_ai_response(api_client):
    """An unknown command via API should succeed with brain as tool_used."""
    fake_reply = _mock_anthropic_message("Bonjour!")

    with patch("app.core.brain.settings") as s, \
         patch("anthropic.Anthropic") as mock_cls:
        s.has_anthropic_key = True
        s.anthropic_api_key = "sk-test-key"
        s.jarvis_ai_provider = "anthropic"
        s.jarvis_ai_model = "claude-haiku-4-5-20251001"
        s.jarvis_ai_max_tokens = 250
        s.jarvis_ai_timeout_seconds = 20

        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = fake_reply

        r = api_client.post("/command", json={"command": "say bonjour in French"})

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["tool_used"] == "brain"


def test_api_command_local_fallback(api_client):
    """Unknown command with no API key returns local fallback via brain."""
    with patch("app.core.brain.settings") as s:
        s.has_anthropic_key = False
        s.jarvis_ai_provider = "anthropic"

        r = api_client.post("/command", json={"command": "completely unknown query xyz"})

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["tool_used"] == "brain"


# ---------------------------------------------------------------------------
# 9. /health does not expose API key
# ---------------------------------------------------------------------------

def test_health_does_not_expose_api_key(api_client):
    r = api_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    raw = r.text
    assert "anthropic_api_key" not in raw
    assert "ANTHROPIC_API_KEY" not in raw
    assert "sk-" not in raw
    assert "brain_configured" in body


def test_root_does_not_expose_api_key(api_client):
    r = api_client.get("/")
    assert r.status_code == 200
    raw = r.text
    assert "anthropic_api_key" not in raw
    assert "sk-" not in raw
    assert "brain_configured" in r.json()


# ---------------------------------------------------------------------------
# 10. StatusResponse includes brain fields
# ---------------------------------------------------------------------------

def test_root_includes_brain_status(api_client):
    r = api_client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert "brain_configured" in body
    assert "ai_provider" in body
    assert "ai_model" in body
    assert isinstance(body["brain_configured"], bool)


def test_health_includes_brain_configured(api_client):
    r = api_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "brain_configured" in body
    assert isinstance(body["brain_configured"], bool)
