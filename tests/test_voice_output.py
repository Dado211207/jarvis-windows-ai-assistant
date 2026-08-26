"""Tests for spoken replies end to end.

The defect these exist for: the desktop app never spoke. "Enable Speech"
on the Voice page set an in-memory flag that only the CLI read, while
/voice/speak gated on an environment setting a packaged-app user cannot
change. Both halves worked in isolation and nothing joined up — the
class of bug that survives because every unit test passes.

So the central test here is not "does the service speak" but "does
turning it on in the app make the app speak", asserted across the
surfaces that have to agree: the tool, the endpoint, and the status the
UI reads back.

No test plays audio: pyttsx3 is always mocked (CLAUDE.md's Phase 3 rule).
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import prime_session


@pytest.fixture
def client():
    from app.api.server import app
    with TestClient(app) as test_client:
        yield prime_session(test_client)


@pytest.fixture(autouse=True)
def _silence_engine():
    """Never touch a real audio device, in any test in this file."""
    engine = MagicMock()
    with patch("pyttsx3.init", return_value=engine):
        yield engine


# ---------------------------------------------------------------------------
# The surfaces agree — the property the old split flag broke
# ---------------------------------------------------------------------------

def test_turning_speech_on_in_the_app_makes_the_app_speak(client):
    """The end-to-end path a user actually takes: flip the switch on the
    Voice page, then send a message and hear it."""
    client.post("/voice/output", json={"enabled": True})

    response = client.post("/voice/speak", json={"text": "a reply"})

    assert response.json()["success"] is True


def test_speech_stays_silent_until_it_is_turned_on(client):
    assert client.post("/voice/speak", json={"text": "a reply"}).json()["success"] is False


def test_the_status_endpoint_reports_what_the_speak_endpoint_enforces(client):
    """These two disagreeing is exactly the original bug."""
    client.post("/voice/output", json={"enabled": True})
    assert client.get("/voice/status").json()["tts_enabled"] is True
    assert client.post("/voice/speak", json={"text": "hi"}).json()["success"] is True

    client.post("/voice/output", json={"enabled": False})
    assert client.get("/voice/status").json()["tts_enabled"] is False
    assert client.post("/voice/speak", json={"text": "hi"}).json()["success"] is False


def test_a_speak_on_command_and_the_toggle_control_the_same_thing(client):
    """Typing "speak on" and flipping the switch must not be two
    independent settings."""
    client.post("/command", json={"command": "speak on"})
    assert client.get("/voice/status").json()["tts_enabled"] is True

    client.post("/command", json={"command": "speak off"})
    assert client.get("/voice/status").json()["tts_enabled"] is False


def test_the_setting_survives_a_new_process(client):
    """A restart re-reads the saved choice rather than resetting to off."""
    from app.voice.tts import TextToSpeechService

    client.post("/voice/output", json={"enabled": True})

    assert TextToSpeechService().output_enabled is True


def test_the_endpoint_returns_the_state_actually_in_effect(client, monkeypatch):
    from app.core import preferences

    monkeypatch.setattr(preferences, "store", lambda key, value: False)

    body = client.post("/voice/output", json={"enabled": True}).json()

    assert body["tts_enabled"] is False, "a setting that could not be saved is not in effect"


def test_changing_voice_output_requires_the_session_token():
    from app.api.server import app

    with TestClient(app) as bare:
        bare.get("/health")
        bare.cookies.clear()
        assert bare.post("/voice/output", json={"enabled": True}).status_code == 403


def test_speech_is_off_by_default(client):
    """CLAUDE.md's Phase 3 rule: users opt in explicitly."""
    assert client.get("/voice/status").json()["tts_enabled"] is False


# ---------------------------------------------------------------------------
# The server holds the gate, not the browser
# ---------------------------------------------------------------------------

def test_a_stale_page_cannot_make_jarvis_talk_after_speech_is_switched_off(client):
    """A tab left open before speech was turned off elsewhere still POSTs
    to /voice/speak. The server must refuse."""
    client.post("/voice/output", json={"enabled": True})
    client.post("/command", json={"command": "speak off"})   # switched off elsewhere

    assert client.post("/voice/speak", json={"text": "stale"}).json()["success"] is False


def test_nothing_is_spoken_without_the_session_token():
    from app.api.server import app

    with TestClient(app) as bare:
        bare.get("/health")
        bare.cookies.clear()
        assert bare.post("/voice/speak", json={"text": "hi"}).status_code == 403


# ---------------------------------------------------------------------------
# The chat page asks for speech, and asks correctly
# ---------------------------------------------------------------------------

def _js() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")


def test_the_chat_page_speaks_a_completed_answer():
    js = _js()
    assert "speakReply" in js
    assert '"/voice/speak"' in js


def test_an_approval_prompt_is_never_read_aloud():
    """Both streaming and one-shot chat pass through one approval gate."""
    js = _js()
    render = js[js.index("function renderCommandResponse"):js.index("async function streamChat")]
    approval = render[:render.index("const reply")]
    assert "requires_approval" in approval
    assert "addApprovalCard" in approval
    assert "return null" in approval
    assert "speakReply" not in approval
    assert 'renderCommandResponse(evt.response || {})' in js
    assert 'renderCommandResponse(await API.post("/command"' in js


def test_a_failed_answer_is_never_spoken():
    js = _js()
    assert "if (!sawError && stream && !stream.isEmpty()) speakReply" in js


def test_speech_failures_never_disturb_the_conversation():
    js = _js()
    speak = js[js.index("async function speakReply"):]
    assert "catch" in speak[:600]


def test_the_client_truncates_to_the_servers_limit():
    """Otherwise a long answer produces a 422 instead of speech."""
    from app.voice.tts import MAX_SPEAK_LENGTH

    js = _js()
    assert f"SPOKEN_REPLY_MAX_CHARS = {MAX_SPEAK_LENGTH}" in js


# ---------------------------------------------------------------------------
# The Voice page describes the whole voice experience
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("element_id", [
    "voice-output-toggle", "voice-output-message", "stt-avail", "stt-model", "stt-detail",
])
def test_the_voice_page_has_its_controls(client, element_id):
    assert f'id="{element_id}"' in client.get("/ui/voice").text


def test_the_voice_page_says_the_choice_is_remembered(client):
    assert "remembered" in client.get("/ui/voice").text


def test_the_voice_page_states_nothing_is_sent_until_you_press_send(client):
    """Transcription fills the box; it never submits on its own."""
    assert "nothing is sent until you press Send" in client.get("/ui/voice").text


def test_the_voice_page_distinguishes_wake_words_from_opt_in_clap_monitoring(client):
    body = client.get("/ui/voice").text
    assert "No wake-word speech recognition" in body
    assert "microphone remains open to measure sound levels" in body
    assert "Privacy Mode" in body
    assert "No wake word or always-listening, ever" not in body


def test_the_voice_page_distinguishes_local_speech_from_elevenlabs(client):
    """Whitespace-normalised, because this is prose in HTML.

    "Privacy Mode blocks every ElevenLabs request" is a single sentence
    in the template but the source wraps between "blocks" and "every", so
    an exact substring search fails on a page that says exactly the right
    thing. Collapsing runs of whitespace tests the copy a user reads
    rather than the column the author happened to wrap at.
    """
    body = " ".join(client.get("/ui/voice").text.split())
    assert "Speech is produced on this computer unless you explicitly select ElevenLabs" in body
    assert "AI-generated voice" in body
    assert "requires internet access" in body
    assert "may incur cost" in body
    assert "Privacy Mode blocks every ElevenLabs request" in body
    assert "nothing is sent to a speech service" not in body


def test_cloud_fallback_notice_survives_engine_service_and_api(client):
    from app.api.routes import tts_service
    from app.voice import engines

    notice = (
        "ElevenLabs could not be reached. "
        "Using the Windows natural voice instead."
    )
    outcome = engines.SpeakOutcome(
        started=True,
        engine=engines.WINDOWS,
        message=notice,
        fallback_message=notice,
    )
    with patch("app.voice.engines.speak", return_value=outcome), \
         patch.object(tts_service, "is_available", return_value=True):
        body = client.post("/voice/speak-once", json={"text": "hello"}).json()

    assert body["success"] is True
    assert body["engine"] == engines.WINDOWS
    assert body["fallback_message"] == notice


def test_chat_keeps_a_successful_fallback_notice_visible():
    js = _js()
    seam = js[js.index("function handleSpeechResponse"):js.index("// The speaker button")]
    assert "fallback_message" in seam
    assert "setChatStatus(fallback)" in seam
    assert "Never erase this" in seam
    assert "handleSpeechResponse(r, btn, true)" in js
    assert "handleSpeechResponse(r, btn, false)" in js
