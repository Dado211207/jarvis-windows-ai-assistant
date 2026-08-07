"""Tests for the push-to-talk REST endpoints (POST /voice/transcribe,
GET /voice/stt-status) — real HTTP round trips against the real app, with
app.voice.stt.stt_service's adapter swapped for a FakeSTTAdapter so no
real audio or model is ever touched.
"""

import glob
import io
import os
import tempfile

import pytest

from app.voice.stt import FakeSTTAdapter, stt_service


@pytest.fixture(scope="module")
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield prime_session(client)


@pytest.fixture(autouse=True)
def fake_stt_adapter():
    fake = FakeSTTAdapter(transcript="turn on the lights")
    stt_service.set_adapter_override(fake)
    yield fake
    stt_service.set_adapter_override(None)


def _fake_audio_bytes(size: int = 512) -> bytes:
    return b"\x00\x01\x02\x03" * (size // 4)


def test_transcribe_returns_fake_transcript(api_client):
    files = {"audio": ("clip.webm", io.BytesIO(_fake_audio_bytes()), "audio/webm")}
    r = api_client.post("/voice/transcribe", files=files)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["text"] == "turn on the lights"


def test_transcribe_rejects_empty_audio(api_client):
    files = {"audio": ("clip.webm", io.BytesIO(b""), "audio/webm")}
    r = api_client.post("/voice/transcribe", files=files)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert "no audio" in body["message"].lower()


def test_transcribe_rejects_oversized_audio(api_client):
    from app.voice.stt import MAX_AUDIO_BYTES
    files = {"audio": ("clip.webm", io.BytesIO(b"\x00" * (MAX_AUDIO_BYTES + 1)), "audio/webm")}
    r = api_client.post("/voice/transcribe", files=files)
    assert r.status_code == 200
    assert r.json()["success"] is False
    assert "too large" in r.json()["message"].lower()


def test_transcribe_requires_session_token():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as fresh_client:
        files = {"audio": ("clip.webm", io.BytesIO(_fake_audio_bytes()), "audio/webm")}
        r = fresh_client.post("/voice/transcribe", files=files)
    assert r.status_code == 403


def test_transcribe_deletes_the_temp_file_afterward(api_client):
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "jarvis_ptt_*")))
    files = {"audio": ("clip.webm", io.BytesIO(_fake_audio_bytes()), "audio/webm")}
    api_client.post("/voice/transcribe", files=files)
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), "jarvis_ptt_*")))
    assert after - before == set(), "a push-to-talk temp recording was left behind"


def test_transcribe_deletes_temp_file_even_when_adapter_reports_failure(api_client, fake_stt_adapter):
    stt_service.set_adapter_override(FakeSTTAdapter(available=False))
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "jarvis_ptt_*")))
    files = {"audio": ("clip.webm", io.BytesIO(_fake_audio_bytes()), "audio/webm")}
    r = api_client.post("/voice/transcribe", files=files)
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), "jarvis_ptt_*")))

    assert r.json()["success"] is False
    assert after - before == set()


def test_stt_status_reflects_fake_adapter(api_client):
    r = api_client.get("/voice/stt-status")
    assert r.status_code == 200
    assert r.json()["available"] is True


def test_stt_status_reflects_unavailable_adapter(api_client, fake_stt_adapter):
    stt_service.set_adapter_override(FakeSTTAdapter(available=False))
    r = api_client.get("/voice/stt-status")
    assert r.json()["available"] is False
    assert r.json()["reason"]


def test_stt_status_requires_no_token_read_only():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as fresh_client:
        r = fresh_client.get("/voice/stt-status")
    assert r.status_code == 200
