"""Tests for TTS voice output.

All tests mock the speech engines so no real audio hardware is required
and nothing is ever played. No real Anthropic API calls are made.

Speech is now a chain of three engines rather than one (see
app/voice/engines.py), so the helpers below pin *which* engine a test is
about instead of letting the machine running the test decide. A
developer with the neural voice installed and a CI runner without it
must get the same answer.
"""

import contextlib
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


@contextlib.contextmanager
def _engine_state(runtime: bool, voice_installed: bool):
    """Pin what each speech tier can do, as app/voice/engines.py sees it.

    Patched through the `engines` module's own attributes rather than by
    importing the target modules again. Several tests here use
    `patch.dict(sys.modules, ...)`, which removes on exit anything that
    was first imported inside the block — and this package is imported
    lazily, so that quietly leaves two live copies of a module around.
    Re-importing would then patch the copy nobody calls, and the test
    would pass or fail depending on which earlier test ran first.
    """
    from app.voice import engines

    with patch.object(engines.kokoro_engine, "runtime_available", return_value=runtime), \
         patch.object(engines.install, "is_installed", return_value=voice_installed), \
         patch.object(engines.winrt_voices, "is_available", return_value=False), \
         patch.dict(sys.modules, {"pyttsx3": None}):
        yield


def _no_engines():
    """Nothing on this machine can speak at all."""
    return _engine_state(runtime=False, voice_installed=False)


def _only_missing_the_voice():
    """Everything is present except the downloaded voice — the state a
    real user is in before they install it. The runtime and the model are
    separated because they produce deliberately different messages: a
    missing model names the download, a missing runtime cannot."""
    return _engine_state(runtime=True, voice_installed=False)


@contextlib.contextmanager
def _sapi5_engine(engine, speaking: bool = False):
    """Install a fake classic engine, and restore module state after —
    it is a process-wide singleton, and a test that leaks it changes the
    answer for every test that runs later."""
    from app.voice import sapi5

    previous_engine = sapi5._engine
    previous_speaking = sapi5._speaking.is_set()
    sapi5._engine = engine
    if speaking:
        sapi5._speaking.set()
    else:
        sapi5._speaking.clear()
    try:
        yield
    finally:
        sapi5._engine = previous_engine
        if previous_speaking:
            sapi5._speaking.set()
        else:
            sapi5._speaking.clear()


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

def test_is_available_is_true_when_only_the_last_resort_engine_can_run():
    """Availability is now "can anything speak", not "is pyttsx3 here".

    Pinned to the classic tier specifically: asserting only that the
    answer is True would also pass on a machine with the neural voice
    installed, which is a different fact and would leave this tier
    untested.
    """
    from app.voice import engines
    from app.voice.tts import TextToSpeechService

    with patch.object(engines.kokoro_engine.engine, "is_ready", return_value=False), \
         patch.object(engines.kokoro_engine, "runtime_available", return_value=False), \
         patch.object(engines.winrt_voices, "is_available", return_value=False), \
         patch.object(engines, "_sapi5_available", return_value=True):
        svc = TextToSpeechService()
        assert svc.is_available() is True
        assert svc.active_engine() == engines.SAPI5


def test_is_available_is_false_only_when_no_engine_at_all_can_run():
    from app.voice import engines
    from app.voice.tts import TextToSpeechService

    with _no_engines():
        svc = TextToSpeechService()
        assert svc.is_available() is False
        assert svc.active_engine() == engines.NONE


# ---------------------------------------------------------------------------
# Speaking leaves a worker running; the next test must not inherit it
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _silence_between_tests():
    """Stop any playback a test started, before the next one looks.

    Several tests here call `speak()`, which hands a chunk stream to the
    audio player's worker thread and returns immediately — by design: a
    request that blocked for the length of a spoken paragraph would have
    timed out. The worker outlives the test that started it, so a later
    test asking "is anything playing?" saw a *correct* True left over from
    its predecessor and `stop()` truthfully answered "Speech stopped."
    where the test expected "TTS is not active."

    Order-dependence, not a product defect — I mis-diagnosed it as one
    first, and the measurement that corrected me is that the leaked
    thread was genuinely alive, not a stale flag.
    """
    yield
    try:
        from app.voice import engines
        engines.stop()
    except Exception:  # noqa: BLE001 - teardown must never fail a test
        pass


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


def test_kokoro_speaks_through_player_with_a_live_cancel_event():
    """Regression: the player must not cancel Kokoro before its first chunk.

    This crosses the real _speak_kokoro -> Player boundary. The model and
    Windows audio device are replaced, but the lazy generator, cancellation
    ownership, worker thread and chunk accounting are the production path.
    """
    from app.voice import audio, engines
    from app.voice.kokoro import engine as kokoro_engine

    player = audio.Player()
    cancel_events = []
    played = []

    def synthesise(_text, voice_key, speed, cancel):
        cancel_events.append(cancel)
        assert cancel.is_set() is False
        yield kokoro_engine.SynthesisChunk(
            samples=[0.0, 0.1, -0.1],
            text="Hello.",
            phonemes="həlˈəʊ",
            seconds=0.01,
        )

    with patch.object(audio, "player", player), \
         patch.object(kokoro_engine.engine, "synthesise", side_effect=synthesise), \
         patch.object(audio, "encode_wav", return_value=b"RIFF-test"), \
         patch.object(player, "_play_one", side_effect=played.append):
        outcome = engines._speak_kokoro("Hello.", "bm_george", 1.0)
        assert player.wait(timeout=2.0)

    assert outcome.started is True
    assert outcome.engine == engines.KOKORO
    assert len(cancel_events) == 1
    assert cancel_events[0].is_set() is False
    assert cancel_events[0] is player.cancel_event()
    assert played == [b"RIFF-test"]
    assert player.state().chunks_played == 1


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


def test_speak_when_no_engine_can_run_returns_failure():
    """Every tier unavailable means no sound, said plainly.

    The machine's own state is patched out rather than relied on: a
    developer who has installed the neural voice must not get a
    different result from CI, and "no engine" is the condition under
    test, not "this laptop happens to lack one".
    """
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()

    with _no_engines():
        result = svc.speak("hello")

    assert result.success is False
    # Stronger than the old substring check: the message has to name the
    # problem, not merely fail.
    assert "no speech engine" in result.message.lower()
    assert "nothing can be spoken" in result.message.lower()


def test_a_missing_voice_says_what_to_install_not_just_that_it_failed():
    """The difference between the two unavailable messages is the whole
    point of having two. "No speech engine is available" is a dead end;
    naming the download is something a person can act on."""
    from app.voice.tts import TextToSpeechService

    with _only_missing_the_voice():
        result = TextToSpeechService().speak("hello")

    assert result.success is False
    assert "not installed" in result.message.lower()
    assert "mb to download" in result.message.lower()
    assert "voice page" in result.message.lower()


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


def test_stop_reaches_the_engine_that_is_speaking():
    """The service no longer owns a speech engine — app/voice/engines.py
    picks one of three. Stop still has to reach whichever is talking."""
    from app.voice import sapi5
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    mock_engine = _make_mock_engine()

    with _sapi5_engine(mock_engine, speaking=True):
        result = svc.stop()

    mock_engine.stop.assert_called_once()
    assert result.success is True
    assert "stopped" in result.message


def test_stop_reaches_every_engine_not_only_the_selected_one():
    """A reply can start on one engine and the selection change under
    it. A Stop that only reached the current choice would leave the
    other one talking."""
    from app.voice import audio
    from app.voice.tts import TextToSpeechService
    mock_engine = _make_mock_engine()

    with _sapi5_engine(mock_engine, speaking=True):
        with patch.object(audio.player, "stop", return_value=True) as player_stop:
            TextToSpeechService().stop()

    player_stop.assert_called_once()
    mock_engine.stop.assert_called_once()


def test_stop_engine_error_returns_failure():
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    mock_engine = _make_mock_engine()
    mock_engine.stop.side_effect = RuntimeError("driver error")

    with _sapi5_engine(mock_engine, speaking=True):
        result = svc.stop()

    assert result.success is False
    assert "Stop failed" in result.message


# ---------------------------------------------------------------------------
# 5. Spoken replies: one flag, remembered
#
# There used to be two — an in-memory session flag only the CLI read, and
# an environment setting a packaged-app user could not change — which is
# why the desktop app never spoke. These tests pin the single flag every
# surface now reads.
# ---------------------------------------------------------------------------

def test_speaking_is_off_by_default():
    """CLAUDE.md's Phase 3 rule: opt-in, never on until asked for."""
    from app.voice.tts import TextToSpeechService
    assert TextToSpeechService().output_enabled is False


def test_turning_speech_on_is_remembered():
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()

    assert svc.set_output_enabled(True) is True
    # A different instance reads the same saved choice — which is what
    # "survives a restart" actually means here.
    assert TextToSpeechService().output_enabled is True


def test_turning_speech_off_is_remembered():
    from app.voice.tts import TextToSpeechService
    svc = TextToSpeechService()
    svc.set_output_enabled(True)

    assert svc.set_output_enabled(False) is False
    assert TextToSpeechService().output_enabled is False


def test_a_saved_choice_overrides_the_configured_default(monkeypatch):
    from app.config import settings
    from app.voice.tts import TextToSpeechService

    monkeypatch.setattr(settings, "jarvis_tts_enabled", True)
    TextToSpeechService().set_output_enabled(False)

    assert TextToSpeechService().output_enabled is False


def test_the_configured_default_applies_when_nothing_was_chosen(monkeypatch):
    from app.config import settings
    from app.voice.tts import TextToSpeechService

    monkeypatch.setattr(settings, "jarvis_tts_enabled", True)

    assert TextToSpeechService().output_enabled is True


def test_an_unsaveable_setting_reports_the_state_actually_in_effect(monkeypatch):
    """A toggle that claims to have changed and then flips back on the
    next page load is worse than one that refuses honestly."""
    from app.core import preferences
    from app.voice.tts import TextToSpeechService

    monkeypatch.setattr(preferences, "store", lambda key, value: False)

    assert TextToSpeechService().set_output_enabled(True) is False


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

def test_speak_on_turns_speaking_on():
    from app.voice import tts as tts_module
    from app.voice.tts import tts_service

    with patch("pyttsx3.init", return_value=_make_mock_engine()):
        result = tts_module._tts_enable()

    assert result["success"] is True
    assert tts_service.output_enabled is True


def test_speak_off_turns_speaking_off():
    from app.voice import tts as tts_module
    from app.voice.tts import tts_service

    tts_service.set_output_enabled(True)
    result = tts_module._tts_disable()

    assert result["success"] is True
    assert tts_service.output_enabled is False


def test_speak_on_with_no_engine_says_nothing_will_be_spoken():
    """The setting really did change and really will produce no sound;
    reporting only the first half would be a half-truth."""
    from app.voice import tts as tts_module
    from app.voice.tts import tts_service

    with _no_engines():
        result = tts_module._tts_enable()

    assert tts_service.output_enabled is True
    # Both halves, asserted separately: the setting did change, and it
    # will still produce no sound. Either one alone is a half-truth.
    assert "voice output is on" in result["message"].lower()
    assert "nothing can be spoken" in result["message"].lower()


def test_tts_status_tool_returns_status_string():
    from app.voice import tts as tts_module
    result = tts_module._tts_status()
    assert result["success"] is True
    assert "Speaks replies" in result["message"]
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
    with _no_engines():
        result = tts_module._tts_test()
    assert result["success"] is False
    assert "no speech engine" in result["message"].lower()


def test_tts_stop_tool_with_nothing_playing():
    """Stopping when nothing is speaking is a no-op, not an error — and
    says so rather than claiming it stopped something."""
    from app.voice import tts as tts_module

    with _sapi5_engine(None, speaking=False):
        result = tts_module._tts_stop()

    assert result["success"] is True
    assert "not active" in result["message"]


# ---------------------------------------------------------------------------
# 8. API endpoints — voice/status, voice/speak, voice/stop
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield prime_session(client)


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
    """When voice output is off, /voice/speak says so in terms a
    packaged-app user can act on — the Voice page, not an env var in a
    .env file they don't have (see
    tests/test_no_developer_instructions_in_ui.py)."""
    r = api_client.post("/voice/speak", json={"text": "hello"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert "turned off" in body["message"].lower()
    assert ".env" not in body["message"]


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
    """With speaking turned on and the engine mocked, /voice/speak
    returns success."""
    from app.voice.tts import tts_service

    tts_service.set_output_enabled(True)
    try:
        with patch("pyttsx3.init", return_value=_make_mock_engine()):
            r = api_client.post("/voice/speak", json={"text": "hello world"})
        assert r.status_code == 200
        assert r.json()["success"] is True
    finally:
        tts_service.set_output_enabled(False)


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


def test_a_worker_that_never_ran_does_not_leave_jarvis_believing_it_speaks():
    """Regression: `is_playing()` must not be a flag only the worker clears.

    The cloud-voice pass replaced a thread-liveness check with
    `_state.playing`, set before the worker starts and cleared only in that
    worker's `finally`. A worker that never started, or died first, left it
    stuck True — so `is_speaking()` never returned False again and every
    Stop answered "Speech stopped." while nothing was playing.

    Reproduced exactly as it was found: set the flag with no live thread.
    """
    from app.voice.audio import Player

    player = Player()
    player._set(playing=True, stopped=False, chunks_played=0, seconds_played=0.0)
    assert player._thread is None
    assert player.is_playing() is False, (
        "a playing flag with no live worker must not report playback"
    )
    # And the counters the state object exists for are still readable.
    assert player.state().playing is True
