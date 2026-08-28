"""Tests for the /onboarding/speech-model/* endpoints (app/api/routes.py).

app.voice.model_installer is mocked throughout — it has its own
dedicated test file (tests/test_model_installer.py) exercising the real
download/verify/install logic against mocked HTTP. This file only
proves the routes call into it correctly and shape responses right.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.voice.model_installer import ModelFileInfo, ModelInfo, InstallState


@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield prime_session(client)


def _fake_info() -> ModelInfo:
    return ModelInfo(
        repo="Systran/faster-whisper-tiny",
        display_name="Whisper tiny (multilingual, CTranslate2)",
        license="mit",
        source_url="https://huggingface.co/Systran/faster-whisper-tiny",
        destination="/fake/path/faster-whisper-tiny",
        language_note="test language note",
        files=[
            ModelFileInfo(name="config.json", size=2249, sha256=None),
            ModelFileInfo(name="model.bin", size=75538270, sha256="abc123"),
        ],
        total_size=75540519,
    )


# ---------------------------------------------------------------------------
# GET /onboarding/speech-model/info
# ---------------------------------------------------------------------------

def test_info_success(api_client):
    with patch("app.voice.model_installer.fetch_model_info", return_value=_fake_info()):
        r = api_client.get("/onboarding/speech-model/info")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["repo"] == "Systran/faster-whisper-tiny"
    assert body["license"] == "mit"
    assert body["total_size"] == 75540519
    assert len(body["files"]) == 2
    model_bin = next(f for f in body["files"] if f["name"] == "model.bin")
    assert model_bin["sha256_verified"] is True
    config = next(f for f in body["files"] if f["name"] == "config.json")
    assert config["sha256_verified"] is False


def test_info_no_auth_required(api_client):
    """Read-only preview — no session token needed, matching every
    other GET status endpoint in this file."""
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with patch("app.voice.model_installer.fetch_model_info", return_value=_fake_info()):
        with TestClient(jarvis_app, raise_server_exceptions=True) as unprimed_client:
            r = unprimed_client.get("/onboarding/speech-model/info")
    assert r.status_code == 200


def test_info_reports_unavailable_when_hf_unreachable(api_client):
    with patch("app.voice.model_installer.fetch_model_info", side_effect=RuntimeError("network down")):
        r = api_client.get("/onboarding/speech-model/info")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["error"]
    assert body["repo"] is None


# ---------------------------------------------------------------------------
# GET /onboarding/speech-model/install-status
# ---------------------------------------------------------------------------

def test_install_status_reflects_installer_state(api_client):
    fake_state = InstallState(status="downloading", current_file="model.bin", bytes_downloaded=1000, bytes_total=75540519, message="…")
    with patch("app.voice.model_installer.model_installer.state", return_value=fake_state):
        r = api_client.get("/onboarding/speech-model/install-status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "downloading"
    assert body["current_file"] == "model.bin"
    assert body["bytes_downloaded"] == 1000


def test_install_status_no_auth_required(api_client):
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as unprimed_client:
        r = unprimed_client.get("/onboarding/speech-model/install-status")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# POST /onboarding/speech-model/install
# ---------------------------------------------------------------------------

def test_install_start_requires_session_token():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as unprimed_client:
        r = unprimed_client.post("/onboarding/speech-model/install")
    assert r.status_code == 403


def test_install_start_calls_installer_start(api_client):
    mock_start = MagicMock(return_value=True)
    with patch("app.voice.model_installer.model_installer.start", mock_start), \
         patch("app.voice.model_installer.model_installer.state", return_value=InstallState(status="checking")):
        r = api_client.post("/onboarding/speech-model/install")
    assert r.status_code == 200
    mock_start.assert_called_once()
    assert r.json()["status"] == "checking"


def test_install_start_when_already_running_still_returns_200(api_client):
    """start() returning False (already running) is not treated as a
    request error — the response reflects the real current state either way."""
    mock_start = MagicMock(return_value=False)
    with patch("app.voice.model_installer.model_installer.start", mock_start), \
         patch("app.voice.model_installer.model_installer.state", return_value=InstallState(status="downloading")):
        r = api_client.post("/onboarding/speech-model/install")
    assert r.status_code == 200
    assert r.json()["status"] == "downloading"


# ---------------------------------------------------------------------------
# POST /onboarding/speech-model/cancel
# ---------------------------------------------------------------------------

def test_install_cancel_requires_session_token():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as unprimed_client:
        r = unprimed_client.post("/onboarding/speech-model/cancel")
    assert r.status_code == 403


def test_install_cancel_calls_installer_cancel(api_client):
    mock_cancel = MagicMock()
    with patch("app.voice.model_installer.model_installer.cancel", mock_cancel), \
         patch("app.voice.model_installer.model_installer.state", return_value=InstallState(status="cancelled")):
        r = api_client.post("/onboarding/speech-model/cancel")
    assert r.status_code == 200
    mock_cancel.assert_called_once()
    assert r.json()["status"] == "cancelled"
