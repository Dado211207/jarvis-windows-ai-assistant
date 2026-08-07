"""Tests for app/core/privacy.py — the v0.2 privacy mode switch.

privacy_mode is a process-wide singleton shared by the whole test suite
(pytest normally runs all test files in one process), so every test here
resets it in both setup and teardown — leaving it ON would silently
change behavior for every test file that happens to run afterward.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.core.privacy import PrivacyModeState, privacy_mode, set_privacy_mode


@pytest.fixture(autouse=True)
def reset_privacy_mode():
    privacy_mode.set(False)
    yield
    privacy_mode.set(False)


# --- PrivacyModeState ---

def test_default_state_is_inactive():
    state = PrivacyModeState()
    assert state.active is False
    assert state.changed_at is None


def test_set_true_activates():
    state = PrivacyModeState()
    state.set(True)
    assert state.active is True


def test_set_stamps_changed_at():
    state = PrivacyModeState()
    assert state.changed_at is None
    state.set(True)
    assert state.changed_at is not None


def test_set_updates_changed_at_even_on_repeat_of_same_value():
    state = PrivacyModeState()
    state.set(True)
    first = state.changed_at
    state.set(True)
    second = state.changed_at
    assert second >= first


# --- set_privacy_mode tool handler ---

def test_set_privacy_mode_tool_turns_on():
    result = set_privacy_mode(True)
    assert result["success"] is True
    assert privacy_mode.active is True
    assert result["data"]["active"] is True


def test_set_privacy_mode_tool_turns_off():
    privacy_mode.set(True)
    result = set_privacy_mode(False)
    assert result["success"] is True
    assert privacy_mode.active is False


def test_privacy_mode_tool_is_registered_with_reversible_risk():
    from app.core.brain import brain
    from app.core.models import PermissionLevel, RiskLevel
    from app.core.tool_registry import registry

    brain.initialise()
    tool = registry.get("set_privacy_mode")
    assert tool is not None
    assert tool.definition.risk == RiskLevel.REVERSIBLE
    assert tool.definition.permission_level == PermissionLevel.SAFE


# --- text command routing ---

def test_privacy_mode_on_command_routes_correctly():
    from app.core.brain import brain
    from app.core.router import CommandRouter
    from app.core.tool_registry import registry

    brain.initialise()
    router = CommandRouter(registry)
    resp = router.route("privacy mode on")
    assert resp.tool_used == "set_privacy_mode"
    assert privacy_mode.active is True


def test_privacy_off_command_routes_correctly():
    from app.core.brain import brain
    from app.core.router import CommandRouter
    from app.core.tool_registry import registry

    brain.initialise()
    privacy_mode.set(True)
    router = CommandRouter(registry)
    resp = router.route("privacy off")
    assert resp.tool_used == "set_privacy_mode"
    assert privacy_mode.active is False


# --- add_memory rejects writes while active ---

def test_add_memory_rejected_when_privacy_mode_active():
    from app.core.memory import add_memory
    privacy_mode.set(True)
    with patch("db.database.get_db") as mock_db:
        result = add_memory("a secret plan")
    assert result["success"] is False
    assert "privacy mode" in result["message"].lower()
    mock_db.return_value.add_memory.assert_not_called()


def test_add_memory_succeeds_when_privacy_mode_inactive():
    from app.core.memory import add_memory
    privacy_mode.set(False)
    with patch("db.database.get_db") as mock_db:
        mock_db.return_value.add_memory.return_value = 1
        result = add_memory("a normal note")
    assert result["success"] is True
    mock_db.return_value.add_memory.assert_called_once()


# --- take_screenshot refuses to capture while active ---

def test_take_screenshot_refused_when_privacy_mode_active():
    from app.desktop.screenshots import take_screenshot
    privacy_mode.set(True)
    with patch("PIL.ImageGrab.grab") as mock_grab:
        result = take_screenshot()
    assert result["success"] is False
    assert "privacy mode" in result["message"].lower()
    mock_grab.assert_not_called()


# --- conversation persistence: real DB round trip ---

@pytest.fixture
def test_db(tmp_path):
    from db.database import Database
    from db.migrations import create_tables
    db_path = tmp_path / "privacy_test.db"
    create_tables(db_path=db_path)
    return Database(db_path=db_path)


def test_conversation_not_persisted_when_privacy_mode_active(test_db):
    from app.core.brain import Brain
    from app.core.router import CommandRouter
    from app.core.tool_registry import ToolRegistry

    privacy_mode.set(True)
    mock_brain = MagicMock(spec=Brain)
    from app.core.models import BrainResponse
    mock_brain.generate_response.return_value = BrainResponse(
        content="a private reply", provider="anthropic", used_api=True,
    )

    with patch("db.database.get_db", return_value=test_db):
        router = CommandRouter(ToolRegistry(), brain=mock_brain)
        router.route("something only the AI would answer")

    assert test_db.get_recent_conversations(limit=50) == []


def test_conversation_is_persisted_when_privacy_mode_inactive(test_db):
    from app.core.brain import Brain
    from app.core.router import CommandRouter
    from app.core.tool_registry import ToolRegistry

    privacy_mode.set(False)
    mock_brain = MagicMock(spec=Brain)
    from app.core.models import BrainResponse
    mock_brain.generate_response.return_value = BrainResponse(
        content="a normal reply", provider="anthropic", used_api=True,
    )

    with patch("db.database.get_db", return_value=test_db):
        router = CommandRouter(ToolRegistry(), brain=mock_brain)
        router.route("a normal question")

    convos = test_db.get_recent_conversations(limit=50)
    assert len(convos) == 2  # user turn + assistant turn


# --- stored memory is never sent to the AI provider (regression) ---

def test_generate_response_never_includes_stored_memory_in_provider_call():
    """app/core/brain.py must never inject stored memory into the
    Anthropic request — true today because no such code path exists at
    all; this guards against a future change adding memory injection
    without also gating it behind privacy mode."""
    from app.core.brain import Brain
    b = Brain()

    with patch("app.core.brain.settings") as s, \
         patch("anthropic.Anthropic") as mock_anthropic_cls, \
         patch("db.database.get_db") as mock_db:
        s.has_anthropic_key = True
        s.anthropic_api_key = "sk-test-key"
        s.jarvis_ai_provider = "anthropic"
        s.jarvis_ai_model = "claude-haiku-4-5-20251001"
        s.jarvis_ai_max_tokens = 250
        s.jarvis_ai_timeout_seconds = 20
        mock_db.return_value.search_memory.return_value = [
            MagicMock(content="a stored secret memory the AI must never see")
        ]

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        content_block = MagicMock()
        content_block.text = "a reply"
        msg = MagicMock()
        msg.content = [content_block]
        mock_client.messages.create.return_value = msg

        b.generate_response("what's the weather?")

    call_kwargs = mock_client.messages.create.call_args.kwargs
    messages_sent = str(call_kwargs.get("messages"))
    assert "a stored secret memory the AI must never see" not in messages_sent


# --- audit trail: privacy toggle recorded without private content ---

def test_privacy_toggle_recorded_in_lifecycle_with_no_private_content(test_db):
    """The set_privacy_mode tool's own input (a boolean) is never
    sensitive to begin with, so its persisted action_lifecycle record
    carries nothing to redact — proven against a real, isolated DB."""
    from app.core.action_lifecycle import propose

    with patch("db.database.get_db", return_value=test_db):
        record = propose("set_privacy_mode", {"active": True})

    assert record.input_summary == {"active": True}


def test_privacy_toggle_publishes_a_ws_event():
    from app.core.brain import brain
    from app.core.events import EventType, event_bus
    from app.core.router import CommandRouter
    from app.core.tool_registry import registry

    brain.initialise()
    last_seq = event_bus.latest_seq()
    with patch("db.database.get_db") as mock_db:
        mock_db.return_value.log_action = MagicMock()
        router = CommandRouter(registry)
        router.route("privacy mode on")

    new_events = event_bus.since(last_seq)
    result_events = [e for e in new_events if e.type == EventType.ACTION_RESULT]
    assert any(e.payload.get("tool_name") == "set_privacy_mode" for e in result_events)


# --- GET /privacy/status ---

def test_privacy_status_endpoint_reflects_state():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app

    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        privacy_mode.set(False)
        r = client.get("/privacy/status")
        assert r.json()["active"] is False

        privacy_mode.set(True)
        r = client.get("/privacy/status")
        assert r.json()["active"] is True
        assert r.json()["changed_at"] is not None


def test_toggling_privacy_mode_via_command_requires_session_token():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        r = client.post("/command", json={"command": "privacy mode on"})
    assert r.status_code == 403
