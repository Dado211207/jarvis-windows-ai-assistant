"""Reading one answer aloud, on purpose.

The owner's requirement after the release-candidate test: every JARVIS
message needs its own accessible speaker control, with stop, replay and
interruption, alongside a persistent "speak every reply" setting — and
no two utterances overlapping.

The two endpoints answer two different questions and are deliberately
gated differently:

  /voice/speak       — read this new reply automatically. Gated on the
                       saved setting, server-side, so a page left open
                       after speech was switched off elsewhere cannot
                       keep narrating. CLAUDE.md's Phase 3 rule.
  /voice/speak-once  — read *this* message because a person just pressed
                       its button. Not gated on that setting: pressing
                       the button is the request, and refusing it is the
                       defect this release exists to fix.

No real audio is played here — the speech service is patched throughout.
"""

from unittest.mock import MagicMock, patch

import pytest

REPO_APP_JS = "app/ui/static/app.js"


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session

    with TestClient(jarvis_app, raise_server_exceptions=True) as test_client:
        yield prime_session(test_client)


@pytest.fixture
def speaking():
    """A speech service that can speak, with the always-speak flag off —
    the state the interesting cases are about."""
    service = MagicMock()
    service.is_available.return_value = True
    service.output_enabled = False
    service.voice_key = "bm_george"
    service.speak.return_value = MagicMock(success=True, message="Speaking: 'x'")
    with patch("app.api.routes.tts_service", service):
        yield service


def _read(path: str) -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# /voice/speak-once — an explicit request is honoured
# ---------------------------------------------------------------------------

def test_pressing_the_button_speaks_even_with_speak_replies_off(client, speaking):
    body = client.post("/voice/speak-once", json={"text": "The disk is 62% full."}).json()

    assert body["success"] is True
    speaking.speak.assert_called_once_with("The disk is 62% full.")


def test_it_does_not_change_the_saved_setting(client, speaking):
    """Speaking one message must not silently switch on narration of
    every future one."""
    client.post("/voice/speak-once", json={"text": "hello"})

    speaking.set_output_enabled.assert_not_called()


def test_an_unavailable_engine_is_refused_with_the_reason_and_the_step(client):
    service = MagicMock()
    service.is_available.return_value = False
    service.voice_key = "bm_george"

    with patch("app.api.routes.tts_service", service), \
         patch("app.voice.engines.unavailable_message",
               return_value="The neural voice is not installed yet. Install it from the Voice page."):
        body = client.post("/voice/speak-once", json={"text": "hello"}).json()

    assert body["success"] is False
    assert "Install it from the Voice page" in body["message"]


def test_it_requires_the_session_token(speaking):
    """Every mutating route does. The gate that was removed is the saved
    setting, not authentication."""
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app

    with TestClient(jarvis_app, raise_server_exceptions=True) as bare:
        response = bare.post("/voice/speak-once", json={"text": "hello"})

    assert response.status_code == 403


def test_the_length_limit_still_applies(client, speaking):
    from app.voice.tts import MAX_SPEAK_LENGTH

    response = client.post("/voice/speak-once", json={"text": "x" * (MAX_SPEAK_LENGTH + 1)})

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# /voice/speak — the automatic path keeps its gate
# ---------------------------------------------------------------------------

def test_automatic_speech_is_still_refused_when_the_setting_is_off(client, speaking):
    """CLAUDE.md's Phase 3 rule, unchanged: a stale page must not be able
    to narrate on its own after speech was switched off elsewhere."""
    body = client.post("/voice/speak", json={"text": "hello"}).json()

    assert body["success"] is False
    speaking.speak.assert_not_called()


def test_automatic_speech_works_when_the_setting_is_on(client, speaking):
    speaking.output_enabled = True

    body = client.post("/voice/speak", json={"text": "hello"}).json()

    assert body["success"] is True


# ---------------------------------------------------------------------------
# /voice/speaking — so a Stop button can stop lying
# ---------------------------------------------------------------------------

def test_speaking_state_is_reported(client):
    with patch("app.voice.engines.is_speaking", return_value=True):
        assert client.get("/voice/speaking").json() == {"speaking": True}

    with patch("app.voice.engines.is_speaking", return_value=False):
        assert client.get("/voice/speaking").json() == {"speaking": False}


def test_an_engine_that_cannot_answer_reports_not_speaking_rather_than_failing(client):
    """A polled endpoint that 500s would leave a Stop button stuck on
    every page in the app."""
    with patch("app.voice.engines.is_speaking", side_effect=OSError("device gone")):
        response = client.get("/voice/speaking")

    assert response.status_code == 200
    assert response.json() == {"speaking": False}


# ---------------------------------------------------------------------------
# The control itself
# ---------------------------------------------------------------------------

def test_every_assistant_message_gets_a_speaker_button():
    js = _read(REPO_APP_JS)

    assert "makeSpeakButton" in js
    assert 'role === "assistant"' in js, "the button belongs on JARVIS's messages, not the user's"


def test_the_button_is_named_for_a_screen_reader():
    js = _read(REPO_APP_JS)

    assert "Read this answer aloud" in js
    assert "Stop reading this answer aloud" in js
    assert "aria-pressed" in js, "a toggle must report which state it is in"


def test_the_glyph_is_hidden_from_assistive_technology():
    """The button already has a name; a screen reader announcing a
    triangle on top of it is noise."""
    js = _read(REPO_APP_JS)

    assert 'glyph.setAttribute("aria-hidden", "true")' in js


def test_a_streaming_answer_gets_its_button_only_once_it_is_complete():
    """A Listen button on half an answer reads half an answer."""
    js = _read(REPO_APP_JS)

    assert "speakBtn.hidden = true" in js
    assert "finish()" in js


def test_nothing_ever_speaks_over_something_else():
    """Every path stops what is playing before starting. A reply arriving
    while an older one is being read must not produce two voices."""
    js = _read(REPO_APP_JS)

    speak_on_demand = js.split("async function speakOnDemand", 1)[1].split("\n}", 1)[0]
    speak_reply = js.split("async function speakReply", 1)[1].split("\n}", 1)[0]

    assert "await stopSpeech();" in speak_on_demand
    assert "await stopSpeech();" in speak_reply
    assert speak_on_demand.index("await stopSpeech();") < speak_on_demand.index("/voice/speak-once")
    assert speak_reply.index("await stopSpeech();") < speak_reply.index("/voice/speak")


def test_pressing_the_button_again_stops_rather_than_restarting():
    js = _read(REPO_APP_JS)
    speak_on_demand = js.split("async function speakOnDemand", 1)[1].split("\n}", 1)[0]

    assert "if (speakingButton === btn) { await stopSpeech(); return; }" in speak_on_demand


def test_the_stop_button_puts_itself_back_when_the_sound_finishes():
    """The service returns as soon as playback starts, so the page has no
    other way to know. A Stop button left showing is a control that lies
    about the state of the machine."""
    js = _read(REPO_APP_JS)

    assert "/voice/speaking" in js
    assert "SPEECH_POLL_MS" in js
    assert "forgetSpeaking" in js


def test_clearing_the_conversation_stops_speech_first():
    """The button that would have stopped it is about to be removed."""
    js = _read(REPO_APP_JS)
    reset = js.split("async function resetConversation", 1)[1].split("\n}", 1)[0]

    assert "await stopSpeech();" in reset


def test_approval_prompts_are_still_never_read_aloud():
    """CLAUDE.md's Phase 3 rule. They are to be read and decided on."""
    js = _read(REPO_APP_JS)
    approval_card = js.split("function addApprovalCard", 1)[1].split("\nfunction ", 1)[0]

    assert "makeSpeakButton" not in approval_card
    assert "speakReply" not in approval_card


# ---------------------------------------------------------------------------
# The persistent setting, reachable where it is used
# ---------------------------------------------------------------------------

def test_the_chat_page_carries_the_speak_replies_switch():
    html = _read("app/ui/templates/chat.html")

    assert 'id="chat-speak-replies"' in html


def test_the_switch_reads_and_writes_the_one_saved_setting():
    """Not a second flag: the same /voice/output the Voice page toggle
    uses, so the two controls cannot disagree."""
    js = _read(REPO_APP_JS)
    init = js.split("async function initSpeakRepliesToggle", 1)[1].split("\n}\n", 1)[0]

    assert '"/voice/status"' in init
    assert '"/voice/output"' in init


def test_switching_it_off_silences_what_is_already_playing():
    js = _read(REPO_APP_JS)
    init = js.split("async function initSpeakRepliesToggle", 1)[1].split("\n}\n", 1)[0]

    assert "if (!wanted) await stopSpeech();" in init


def test_the_switch_is_honest_when_no_voice_is_installed():
    js = _read(REPO_APP_JS)

    assert "Speak replies (no voice installed)" in js


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------

def test_push_to_talk_has_a_hard_recording_limit_and_clears_its_timer():
    js = _read(REPO_APP_JS)
    ptt = js.split("// ── Push-to-talk", 1)[1].split("// ──", 1)[0]

    assert "PTT_MAX_RECORDING_MS = 60 * 1000" in ptt
    assert "pttRecordingTimer = setTimeout" in ptt
    assert "pttClearRecordingTimer()" in ptt


def test_push_to_talk_tears_down_on_pagehide_and_ignores_a_late_microphone():
    js = _read(REPO_APP_JS)

    assert 'window.addEventListener("pagehide", () => {' in js
    pagehide = js.split('window.addEventListener("pagehide", () => {', 1)[1].split("});", 1)[0]
    assert "pttCancel();" in pagehide
    assert "requestGeneration !== pttRequestGeneration" in js
    assert "stream.getTracks().forEach(track => track.stop())" in js


def test_push_to_talk_handles_media_recorder_errors():
    js = _read(REPO_APP_JS)

    assert 'addEventListener("error", pttOnRecorderError)' in js
    assert "function pttOnRecorderError()" in js
    assert "pttReleaseMicrophone();" in js


def test_no_innerhtml_anywhere():
    """CLAUDE.md's Phase 4 rule, permanently."""
    assert "innerHTML" not in _read(REPO_APP_JS)


def test_no_secrets_in_the_new_code():
    """A bare "sk-" substring search matches this file's own
    `risk-<level>` class names, so the pattern is the shape of a real
    key rather than its first three characters."""
    import re

    js = _read(REPO_APP_JS)

    assert "ANTHROPIC_API_KEY" not in js
    assert not re.search(r"sk-(ant|[A-Za-z0-9_]{16,})", js)
