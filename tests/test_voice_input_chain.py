"""The installed push-to-talk chain, end to end, with real audio bytes.

Real Windows hardware reported "Speech runtime — Not ready" and a
push-to-talk button that did nothing after setup. Three causes, all
covered here:

  1. faster-whisper was deliberately excluded from the installer, so the
     packaged app could never transcribe anything (see
     tests/test_packaging_spec.py for the packaging half).
  2. The on/off setting was environment-only and defaulted off, so the
     packaged app had no way to turn voice input on — while the status
     message told users to "turn it on from the Voice page", where no
     such control existed.
  3. A single "Not ready" line covered four different problems, so a
     failure could not be acted on.

Audio here is a **real WAV**, synthesised deterministically in-process:
the same bytes on every machine and every run, with no fixture file to
drift and no microphone involved. That exercises the parts CI can
genuinely exercise — upload, size limits, the temp file and its deletion,
cancellation, the error envelope — rather than mocking them away.

What CI still cannot exercise, stated plainly: a real microphone, real
WebView2 getUserMedia, and real Whisper inference. Those need physical
Windows hardware, and no test in this file claims otherwise — the
adapter is the deterministic fake, and
test_no_test_here_runs_real_inference proves it.
"""

import io
import math
import struct
import wave

import pytest

from app.voice.stt import FakeSTTAdapter, stt_service

SAMPLE_RATE = 16000
TONE_HZ = 440


def synthetic_wav(seconds: float = 0.5, amplitude: float = 0.3) -> bytes:
    """A deterministic mono 16-bit PCM tone.

    Deliberately generated rather than committed: a binary fixture in git
    is a thing nobody can review, and this is reproducible byte-for-byte
    from four numbers.
    """
    frames = int(SAMPLE_RATE * seconds)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(b"".join(
            struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * TONE_HZ * i / SAMPLE_RATE)))
            for i in range(frames)
        ))
    return buffer.getvalue()


@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session

    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield prime_session(client)


@pytest.fixture
def fake_adapter():
    adapter = FakeSTTAdapter(transcript="open notepad")
    stt_service.set_adapter_override(adapter)
    yield adapter
    stt_service.set_adapter_override(None)


# ---------------------------------------------------------------------------
# The synthetic fixture itself
# ---------------------------------------------------------------------------

def test_the_fixture_is_a_real_parseable_wav():
    with wave.open(io.BytesIO(synthetic_wav()), "rb") as source:
        assert source.getnchannels() == 1
        assert source.getframerate() == SAMPLE_RATE
        assert source.getnframes() == int(SAMPLE_RATE * 0.5)


def test_the_fixture_is_byte_for_byte_deterministic():
    """A fixture that varies between runs turns a real failure into a
    flake, and a flake into something nobody trusts."""
    assert synthetic_wav() == synthetic_wav()


# ---------------------------------------------------------------------------
# Upload → transcribe → cleanup
# ---------------------------------------------------------------------------

def test_real_audio_bytes_reach_the_adapter_and_come_back_as_text(api_client, fake_adapter):
    r = api_client.post(
        "/voice/transcribe",
        files={"audio": ("recording.wav", synthetic_wav(), "audio/wav")},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["text"] == "open notepad"
    assert len(fake_adapter.calls) == 1


def test_the_uploaded_clip_is_written_and_then_deleted(api_client):
    """CLAUDE.md: one temp file per recording, deleted immediately after
    — success or failure. Asserted by capturing the path the adapter was
    handed and checking it is gone afterwards, not by trusting the code
    to have called unlink."""
    seen = {}

    class _PathCapturingAdapter(FakeSTTAdapter):
        def transcribe(self, audio_path, timeout_seconds=30.0):
            seen["path"] = audio_path
            seen["existed"] = audio_path.exists()
            seen["bytes"] = audio_path.read_bytes()
            return super().transcribe(audio_path, timeout_seconds)

    stt_service.set_adapter_override(_PathCapturingAdapter())
    try:
        api_client.post("/voice/transcribe", files={"audio": ("r.wav", synthetic_wav(), "audio/wav")})
    finally:
        stt_service.set_adapter_override(None)

    assert seen["existed"] is True, "the adapter must be handed a real file"
    assert seen["bytes"] == synthetic_wav(), "the audio must arrive unaltered"
    assert not seen["path"].exists(), "the recording must not survive the request"


def test_the_clip_is_deleted_even_when_transcription_fails(api_client):
    seen = {}

    class _ExplodingAdapter(FakeSTTAdapter):
        def transcribe(self, audio_path, timeout_seconds=30.0):
            seen["path"] = audio_path
            raise RuntimeError("model exploded")

    stt_service.set_adapter_override(_ExplodingAdapter())
    try:
        with pytest.raises(RuntimeError):
            api_client.post("/voice/transcribe", files={"audio": ("r.wav", synthetic_wav(), "audio/wav")})
    finally:
        stt_service.set_adapter_override(None)

    assert not seen["path"].exists(), "a failed transcription must not leave audio on disk"


def test_an_empty_upload_is_refused_without_touching_the_adapter(api_client, fake_adapter):
    r = api_client.post("/voice/transcribe", files={"audio": ("r.wav", b"", "audio/wav")})

    assert r.json()["success"] is False
    assert fake_adapter.calls == []


def test_an_oversized_upload_is_refused(api_client, fake_adapter):
    from app.voice.stt import MAX_AUDIO_BYTES

    r = api_client.post(
        "/voice/transcribe",
        files={"audio": ("r.wav", b"\0" * (MAX_AUDIO_BYTES + 1), "audio/wav")},
    )

    assert r.json()["success"] is False
    assert "too large" in r.json()["message"].lower()
    assert fake_adapter.calls == []


def test_transcribing_requires_the_session_token():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app

    with TestClient(jarvis_app, raise_server_exceptions=True) as unprimed:
        r = unprimed.post("/voice/transcribe", files={"audio": ("r.wav", synthetic_wav(), "audio/wav")})
    assert r.status_code == 403


def test_a_disabled_feature_refuses_before_any_audio_is_processed(api_client, fake_adapter):
    """Turning voice input off must actually stop transcription, not just
    hide a button."""
    from app.core.preferences import store

    store("stt_enabled", "false")
    r = api_client.post("/voice/transcribe", files={"audio": ("r.wav", synthetic_wav(), "audio/wav")})

    assert r.json()["success"] is False
    assert fake_adapter.calls == [], "no audio may be processed while the feature is off"


# ---------------------------------------------------------------------------
# The on/off control that did not exist
# ---------------------------------------------------------------------------

def test_voice_input_is_offered_by_default():
    """It shipped off, with no way to switch it on, and that is what
    "push-to-talk still did not function" came down to."""
    from app.config import Settings

    assert Settings().jarvis_stt_enabled is True


def test_the_toggle_turns_it_off_and_on_again(api_client):
    off = api_client.post("/voice/input-enabled", json={"enabled": False})
    assert off.status_code == 200
    assert off.json()["enabled"] is False
    assert off.json()["available"] is False

    on = api_client.post("/voice/input-enabled", json={"enabled": True})
    assert on.json()["enabled"] is True


def test_the_toggle_requires_the_session_token():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app

    with TestClient(jarvis_app, raise_server_exceptions=True) as unprimed:
        r = unprimed.post("/voice/input-enabled", json={"enabled": True})
    assert r.status_code == 403


def test_a_saved_choice_beats_the_environment(monkeypatch):
    from app.core.preferences import store
    from app.voice import stt

    monkeypatch.setattr(stt, "__name__", stt.__name__)  # no-op, keeps the import used
    from app.config import settings

    monkeypatch.setattr(settings, "jarvis_stt_enabled", True, raising=False)
    store("stt_enabled", "false")
    assert stt.input_enabled() is False

    monkeypatch.setattr(settings, "jarvis_stt_enabled", False, raising=False)
    store("stt_enabled", "true")
    assert stt.input_enabled() is True


# ---------------------------------------------------------------------------
# Diagnostics: four problems, four answers
# ---------------------------------------------------------------------------

def test_diagnostics_separates_the_switch_from_the_engine(api_client, fake_adapter):
    """"You switched it off" and "the engine is missing" are different
    problems. One "Not ready" line for both is what made the reported
    failure impossible to act on."""
    from app.core.preferences import store

    store("stt_enabled", "false")
    body = api_client.get("/voice/diagnostics").json()

    assert body["enabled"] is False
    assert body["available"] is False
    assert body["runtime_ready"] is True, "the engine is fine; only the switch is off"


def test_diagnostics_reports_where_the_model_is(api_client, fake_adapter):
    body = api_client.get("/voice/diagnostics").json()

    assert body["model_ready"] is True
    assert body["model_path"], "a path is what separates 'no model' from 'a model elsewhere'"


def test_diagnostics_reports_a_missing_engine_distinctly(api_client):
    stt_service.set_adapter_override(FakeSTTAdapter(available=False))
    try:
        body = api_client.get("/voice/diagnostics").json()
    finally:
        stt_service.set_adapter_override(None)

    assert body["runtime_ready"] is False
    assert body["model_ready"] is False
    assert body["model_path"] == ""


def test_diagnostics_requires_a_session_token(api_client):
    """This replaces an earlier assertion that reading a status needs no
    token. /voice/diagnostics reports the model path under %LOCALAPPDATA%
    — a full Windows user path contains the account name — so it is a
    personal read and is protected like the rest of them. The token is no
    longer only a mutation token."""
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app

    with TestClient(jarvis_app, raise_server_exceptions=True) as unprimed:
        assert unprimed.get("/voice/diagnostics").status_code == 403


def test_diagnostics_leaks_no_secret(api_client, monkeypatch, fake_adapter):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-appear")
    assert "sk-" not in api_client.get("/voice/diagnostics").text


# ---------------------------------------------------------------------------
# Honesty
# ---------------------------------------------------------------------------

def test_no_test_here_runs_real_inference():
    """CLAUDE.md, and the owner's instruction not to claim verification
    that did not happen: every test in this file uses the deterministic
    fake adapter. Real Whisper inference and a real microphone need
    physical Windows hardware."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[-1]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    # Checked by import rather than by substring: a substring check would
    # match the very line asserting it, which proves nothing.
    assert not imported & {"FasterWhisperAdapter", "WhisperModel", "faster_whisper"}


def test_the_still_unverified_parts_are_written_down():
    """A limitation that only exists in a commit message is a limitation
    nobody will find."""
    from pathlib import Path

    source = Path(__file__).read_text(encoding="utf-8")

    assert "real microphone" in source.lower()
    assert "physical Windows hardware" in source
