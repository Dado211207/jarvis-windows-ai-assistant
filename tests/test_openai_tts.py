"""OpenAI Speech tests use httpx.MockTransport only; never the real API."""

import json
import threading
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.voice import openai_tts

WAV = (
    b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt "
    + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
    + (1).to_bytes(2, "little") + (24000).to_bytes(4, "little")
    + (48000).to_bytes(4, "little") + (2).to_bytes(2, "little")
    + (16).to_bytes(2, "little") + b"data" + (0).to_bytes(4, "little")
)
KEY = "unit-test-voice-key"


def _mock_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def make_client(_timeout):
        return httpx.Client(
            transport=transport,
            base_url=openai_tts.API_BASE,
            follow_redirects=False,
        )

    monkeypatch.setattr(openai_tts, "_client", make_client)


def test_exact_destination_payload_and_authorization(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        payload = json.loads(request.content)
        assert request.method == "POST"
        assert request.url == httpx.URL("https://api.openai.com/v1/audio/speech")
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert payload == {
            "model": "gpt-4o-mini-tts",
            "input": "Hello there.",
            "voice": "cedar",
            "instructions": "Calm and clear.",
            "response_format": "wav",
            "speed": 1.0,
        }
        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=WAV)

    _mock_client(monkeypatch, handler)
    assert openai_tts.synthesise_wav(
        "Hello there.", KEY, instructions="Calm and clear.",
    ) == WAV
    assert len(seen) == 1


@pytest.mark.parametrize("model", ["tts-1", "gpt-4o-mini-tts-2025-12-15", "other"])
def test_model_allowlist_rejects_everything_not_initially_enabled(model):
    with pytest.raises(ValueError):
        openai_tts.validate_model(model)


def test_voice_allowlist_and_bounds():
    assert openai_tts.validate_voice("CEDAR") == "cedar"
    assert "onyx" in openai_tts.VOICES
    with pytest.raises(ValueError):
        openai_tts.validate_voice("actor-clone")
    assert openai_tts.clamp_speed(0) == 0.25
    assert openai_tts.clamp_speed(9) == 4.0
    assert len(openai_tts.normalise_instructions("x" * 5000)) == 4096


@pytest.mark.parametrize(
    ("status", "body", "category"),
    [
        (401, b'{"error":"bad key"}', openai_tts.INVALID_KEY),
        (429, b'{"code":"insufficient_quota"}', openai_tts.QUOTA),
        (429, b'{"error":"slow down"}', openai_tts.RATE_LIMITED),
    ],
)
def test_status_errors_are_classified_without_exposing_bodies(
    monkeypatch, status, body, category,
):
    _mock_client(
        monkeypatch,
        lambda _request: httpx.Response(
            status, headers={"content-type": "application/json"}, content=body,
        ),
    )
    with pytest.raises(openai_tts.OpenAITTSError) as caught:
        openai_tts.synthesise_wav("hello", KEY)
    assert caught.value.category == category
    assert KEY not in caught.value.message
    assert body.decode() not in caught.value.message


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (httpx.ReadTimeout("late"), openai_tts.TIMEOUT),
        (httpx.ConnectError("offline"), openai_tts.OFFLINE),
    ],
)
def test_transport_errors_are_honestly_classified(monkeypatch, error, category):
    def handler(_request):
        raise error

    _mock_client(monkeypatch, handler)
    with pytest.raises(openai_tts.OpenAITTSError) as caught:
        openai_tts.synthesise_wav("hello", KEY)
    assert caught.value.category == category


def test_redirect_is_never_followed(monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://example.com/steal"})

    _mock_client(monkeypatch, handler)
    with pytest.raises(openai_tts.OpenAITTSError) as caught:
        openai_tts.synthesise_wav("hello", KEY)
    assert caught.value.category == openai_tts.BAD_RESPONSE
    assert calls == ["https://api.openai.com/v1/audio/speech"]


def test_response_size_limit(monkeypatch):
    monkeypatch.setattr(openai_tts, "MAX_AUDIO_BYTES", 8)
    _mock_client(
        monkeypatch,
        lambda _request: httpx.Response(
            200, headers={"content-type": "audio/wav"}, content=WAV,
        ),
    )
    with pytest.raises(openai_tts.OpenAITTSError) as caught:
        openai_tts.synthesise_wav("hello", KEY)
    assert caught.value.category == openai_tts.BAD_RESPONSE


def test_pre_cancelled_request_makes_zero_transport_calls(monkeypatch):
    calls = []
    _mock_client(monkeypatch, lambda request: calls.append(request))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(openai_tts.OpenAITTSError) as caught:
        openai_tts.synthesise_wav("hello", KEY, cancel=cancel)
    assert caught.value.category == openai_tts.CANCELLED
    assert calls == []


def test_privacy_mode_refuses_before_key_load_or_provider_call():
    from app.core.privacy import privacy_mode
    from app.voice import engines

    privacy_mode.set(True)
    with patch("app.core.credentials.get_openai_key") as get_key, \
         patch("app.voice.openai_tts.synthesise_wav") as transport, \
         patch.object(engines, "openai_fallback_allowed", return_value=False):
        outcome = engines._speak_openai("private reply")
    privacy_mode.set(False)

    assert outcome.started is False
    assert "Privacy mode" in outcome.message
    get_key.assert_not_called()
    transport.assert_not_called()


def test_delayed_response_cannot_play_after_stop():
    from app.voice import engines

    cancel = threading.Event()
    player = MagicMock()
    player.begin_utterance.return_value = cancel
    player.play_wav_bytes_if_current.return_value = False
    player.is_current.return_value = False
    with patch("app.core.credentials.get_openai_key", return_value=KEY), \
         patch("app.core.privacy.privacy_mode.active", False), \
         patch("app.voice.audio.player", player), \
         patch("app.voice.openai_tts.synthesise_wav", return_value=WAV), \
         patch.object(engines, "selected_openai_model", return_value="gpt-4o-mini-tts"), \
         patch.object(engines, "selected_openai_voice", return_value="cedar"), \
         patch.object(engines, "selected_openai_speed", return_value=1.0), \
         patch.object(engines, "selected_openai_instructions", return_value="calm"):
        outcome = engines._speak_openai("reply")

    assert outcome.started is False
    assert "stopped" in outcome.message.lower()
    player.play_wav_bytes_if_current.assert_called_once_with(WAV, cancel)


def test_privacy_fallback_is_explicitly_disclosed():
    from app.core.privacy import privacy_mode
    from app.voice import engines

    local = engines.SpeakOutcome(True, engines.WINDOWS, "Speaking.")
    privacy_mode.set(True)
    with patch.object(engines, "selected_engine", return_value=engines.OPENAI), \
         patch.object(engines, "openai_fallback_allowed", return_value=True), \
         patch.object(engines, "active_engine", return_value=engines.WINDOWS), \
         patch.object(engines, "_speak_windows", return_value=local):
        outcome = engines.speak("reply")
    privacy_mode.set(False)

    assert outcome.started is True
    assert outcome.engine == engines.WINDOWS
    assert "Privacy mode" in outcome.message
    assert "instead" in outcome.message


def test_all_three_ab_buttons_use_exactly_the_same_sentence():
    from app.api.voice_routes import TEST_PHRASE as local_phrase
    from app.voice.elevenlabs import TEST_PHRASE as elevenlabs_phrase
    from app.voice.openai_tts import TEST_PHRASE as openai_phrase

    assert local_phrase == elevenlabs_phrase == openai_phrase
