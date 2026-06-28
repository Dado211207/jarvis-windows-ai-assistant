"""Tests for Phase 3 TTS voice output.

All tests mock pyttsx3 so no real audio hardware is required.
No real Anthropic API calls are made.
"""

import sys
import threading
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_engine():
    engine = MagicMock()
    engine.say = MagicMock()
    engine.runAndWait = MagicMock()
    engine.stop = MagicMock()
    engine.setProperty = MagicMock()
    return engine


# ---------------------------------------------------------------------------
# 1. Config defaults
# ---------------------------------------------------------------------------

def test_tts_disabled_by_default():
    from app.config import settings
    assert settings.jarvis_tts_enabled is False


def test_tts_engine_default():
    from app.config import settings
    assert settings.jarvis_tts_engine == "pyttsx3"


def test_tts_rate_default():
    from app.config import settings
    assert settings.jarvis_tts_rate == 175


def test_tts_volume_default():
    from app.config import settings
    assert settings.jarvis_tts_volume == 1.0


def test_tts_voice_default_empty():
    from app.config import settings
    assert settings.jarvis_tts_voice == ""


# ---------------------------------------------------------------------------
# 2. TextToSpeechService — availability
# ---------------------------------------------------------------------------

def test_is_available_true_when_pyttsx3_importable():
    """is_available() returns True when pyttsx3 can be imported."""
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    mock_module = MagicMock()
    with patch.dict(sys.modules, {"pyttsx3": mock_module}):
        assert svc.is_available() is True


def test_is_available_false_when_pyttsx3_missing():
    """is_available() returns False when pyttsx3 is absent."""
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    with patch.dict(sys.modules, {"pyttsx3": None}):
        assert svc.is_available() is False


# ---------------------------------------------------------------------------
# 3. TextToSpeechService — speak()
# ---------------------------------------------------------------------------

def test_speak_returns_success_with_mocked_engine():
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    mock_engine = _make_mock_engine()

    with patch("pyttsx3.init", return_value=mock_engine):
        result = svc.speak("Hello JARVIS")

    assert result.success is True
    assert "Speaking" in result.message


def test_speak_empty_text_returns_failure():
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    result = svc.speak("")
    assert result.success is False
    assert "Nothing" in result.message


def test_speak_whitespace_only_returns_failure():
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    result = svc.speak("   ")
    assert result.success is False


def test_speak_text_too_long_returns_failure():
    from app.voice.tts import TextToSpeechService, MAX_SPEAK_LENGTH
    svc = TextToSpeechService()
    long_text = "x" * (MAX_SPEAK_LENGTH + 1)
    result = svc.speak(long_text)
    assert result.success is False
    assert "too long" in result.message


def test_speak_exactly_max_length_succeeds():
    from app.voice.tts import TextToSpeechService, MAX_SPEAK_LENGTH
    svc = TextToSpeechService()
    mock_engine = _make_mock_engine()
    text = "a" * MAX_SPEAK_LENGTH
    with patch("pyttsx3.init", return_value=mock_engine):
        result = svc.speak(text)
    assert result.success is True


def test_speak_when_pyttsx3_unavailable_returns_failure():
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    with patch.dict(sys.modules, {"pyttsx3": None}):
        result = svc.speak("hello")
    assert result.success is False
    assert "not available" in result.message


def test_speak_engine_init_failure_is_handled_gracefully():
    """Engine init failure in the daemon thread must not crash the caller."""
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    mock_pyttsx3 = MagicMock()
    mock_pyttsx3.init.side_effect = RuntimeError("no audio device")

    with patch.dict(sys.modules, {"pyttsx3": mock_pyttsx3}):
        result = svc.speak("hello")

    # Returns success immediately (async thread may fail silently)
    assert result.success is True
    # Wait briefly for daemon thread to finish so test teardown is clean
    threading.Event().wait(0.05)


# ---------------------------------------------------------------------------
# 4. TextToSpeechService — stop()
# ---------------------------------------------------------------------------

def test_stop_when_engine_not_initialised_returns_success():
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    result = svc.stop()
    assert result.success is True
    assert "not active" in result.message


def test_stop_calls_engine_stop():
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    mock_engine = _make_mock_engine()
    svc._engine = mock_engine

    result = svc.stop()

    mock_engine.stop.assert_called_once()
    assert result.success is True
    assert "stopped" in result.message


def test_stop_engine_error_returns_failure():
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    mock_engine = _make_mock_engine()
    mock_engine.stop.side_effect = RuntimeError("driver error")
    svc._engine = mock_engine

    result = svc.stop()

    assert result.success is False
    assert "Stop failed" in result.message


# ---------------------------------------------------------------------------
# 5. Session enable / disable
# ---------------------------------------------------------------------------

def test_session_disabled_by_default():
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    assert svc.session_enabled is False


def test_session_enable_sets_flag():
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    svc.session_enabled = True
    assert svc.session_enabled is True


def test_session_disable_clears_flag():
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    svc.session_enabled = True
    svc.session_enabled = False
    assert svc.session_enabled is False


# ---------------------------------------------------------------------------
# 6. Router — TTS command routing
# ---------------------------------------------------------------------------

def _route_tts(cmd: str, expected_tool: str) -> None:
    from app.core.tool_registry import ToolRegistry
    from app.core.models import PermissionLevel, ToolCategory, ToolDefinition
    from app.core.router import CommandRouter

    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name=expected_tool,
            description="tts smoke",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.VOICE,
        ),
        lambda: {"success": True, "message": "ok", "data": None},
    )
    with patch("db.database.get_db") as mock_db:
        mock_db.return_value.log_action = MagicMock()
        resp = CommandRouter(reg).route(cmd)

    assert resp.tool_used == expected_tool, (
        f"'{cmd}' -> '{resp.tool_used}', expected '{expected_tool}'"
    )


def test_router_speak_on():
    _route_tts("speak on", "tts_enable")


def test_router_speak_off():
    _route_tts("speak off", "tts_disable")


def test_router_speak_status():
    _route_tts("speak status", "tts_status")


def test_router_speak_test():
    _route_tts("speak test", "tts_test")


def test_router_stop_speaking():
    _route_tts("stop speaking", "tts_stop")


def test_router_speak_on_case_insensitive():
    _route_tts("SPEAK ON", "tts_enable")


# ---------------------------------------------------------------------------
# 7. Tool handlers
# ---------------------------------------------------------------------------

def test_tts_enable_tool_sets_session_flag():
    from app.voice import tts as tts_module
    from app.voice.tts import tts_service

    original = tts_service.session_enabled
    try:
        mock_engine = _make_mock_engine()
        with patch("pyttsx3.init", return_value=mock_engine):
            result = tts_module._tts_enable()
        assert result["success"] is True
        assert tts_service.session_enabled is True
    finally:
        tts_service.session_enabled = original


def test_tts_disable_tool_clears_session_flag():
    from app.voice import tts as tts_module
    from app.voice.tts import tts_service

    tts_service.session_enabled = True
    result = tts_module._tts_disable()
    assert result["success"] is True
    assert tts_service.session_enabled is False


def test_tts_status_tool_returns_status_string():
    from app.voice import tts as tts_module
    result = tts_module._tts_status()
    assert result["success"] is True
    assert "TTS" in result["message"]
    assert "Engine" in result["message"]


def test_tts_test_tool_with_mocked_engine():
    from app.voice import tts as tts_module
    mock_engine = _make_mock_engine()
    with patch("pyttsx3.init", return_value=mock_engine):
        result = tts_module._tts_test()
    assert result["success"] is True
    assert "Speaking" in result["message"] or "test phrase" in result["message"]


def test_tts_test_tool_when_unavailable():
    from app.voice import tts as tts_module
    with patch.dict(sys.modules, {"pyttsx3": None}):
        result = tts_module._tts_test()
    assert result["success"] is False
    assert "not available" in result["message"]


def test_tts_stop_tool_with_no_engine():
    from app.voice import tts as tts_module
    from app.voice.tts import TextToSpeechService, tts_service

    original_engine = tts_service._engine
    tts_service._engine = None
    try:
        result = tts_module._tts_stop()
        assert result["success"] is True
    finally:
        tts_service._engine = original_engine


# ---------------------------------------------------------------------------
# 8. API endpoints — voice/status, voice/speak, voice/stop
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield client


def test_voice_status_returns_200(api_client):
    r = api_client.get("/voice/status")
    assert r.status_code == 200
    body = r.json()
    assert "tts_enabled" in body
    assert "tts_engine" in body
    assert "tts_available" in body


def test_voice_status_tts_disabled_by_default(api_client):
    r = api_client.get("/voice/status")
    assert r.status_code == 200
    body = r.json()
    assert body["tts_enabled"] is False


def test_voice_status_no_secrets_exposed(api_client):
    r = api_client.get("/voice/status")
    raw = r.text
    assert "ANTHROPIC_API_KEY" not in raw
    assert "sk-" not in raw


def test_voice_speak_disabled_returns_disabled_message(api_client):
    """When TTS is disabled in config, /voice/speak returns a clear message."""
    r = api_client.post("/voice/speak", json={"text": "hello"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert "disabled" in body["message"].lower()


def test_voice_speak_empty_text_returns_422(api_client):
    r = api_client.post("/voice/speak", json={"text": ""})
    assert r.status_code == 422


def test_voice_speak_whitespace_text_returns_422(api_client):
    r = api_client.post("/voice/speak", json={"text": "   "})
    assert r.status_code == 422


def test_voice_speak_text_too_long_returns_422(api_client):
    from app.voice.tts import MAX_SPEAK_LENGTH
    r = api_client.post("/voice/speak", json={"text": "x" * (MAX_SPEAK_LENGTH + 1)})
    assert r.status_code == 422


def test_voice_speak_when_enabled_and_available(api_client):
    """With TTS enabled and engine mocked, /voice/speak returns success."""
    from app.config import settings
    mock_engine = _make_mock_engine()
    original = settings.jarvis_tts_enabled
    try:
        settings.jarvis_tts_enabled = True
        with patch("pyttsx3.init", return_value=mock_engine):
            r = api_client.post("/voice/speak", json={"text": "hello world"})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
    finally:
        settings.jarvis_tts_enabled = original


def test_voice_stop_returns_200(api_client):
    r = api_client.post("/voice/stop")
    assert r.status_code == 200
    body = r.json()
    assert "success" in body
    assert "message" in body


def test_voice_speak_missing_text_field_returns_422(api_client):
    r = api_client.post("/voice/speak", json={})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 9. TTS tools registered in brain
# ---------------------------------------------------------------------------

def test_tts_tools_registered_after_brain_init():
    from app.core.brain import brain
    from app.core.tool_registry import registry
    brain.initialise()
    names = {t.name for t in registry.list_definitions()}
    tts_tools = {"tts_enable", "tts_disable", "tts_status", "tts_test", "tts_stop"}
    missing = tts_tools - names
    assert not missing, f"TTS tools missing from registry: {missing}"


def test_registry_has_at_least_14_tools_after_phase3_init():
    from app.core.brain import brain
    from app.core.tool_registry import registry
    brain.initialise()
    assert len(registry) >= 14, f"Expected ≥14 tools (9 + 5 TTS), got {len(registry)}"


# ---------------------------------------------------------------------------
# 10. Regression — existing 83 tests baseline (routing)
# ---------------------------------------------------------------------------

def test_existing_help_command_still_works(api_client):
    r = api_client.post("/command", json={"command": "help"})
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_existing_status_command_still_works(api_client):
    r = api_client.post("/command", json={"command": "status"})
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_existing_system_status_command_still_works(api_client):
    r = api_client.post("/command", json={"command": "system status"})
    assert r.status_code == 200
    assert r.json()["success"] is True
