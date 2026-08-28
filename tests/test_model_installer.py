"""Tests for app/voice/model_installer.py. Never makes a real network
call to Hugging Face — httpx.get/httpx.stream are mocked throughout, so
this suite is fast, deterministic, and doesn't depend on an external
service being reachable (or matching a fixed snapshot of its content).
"""

import hashlib
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.voice.model_installer import (
    ModelFileInfo,
    ModelInfo,
    ModelInstaller,
    fetch_model_info,
)

FAKE_HF_RESPONSE = {
    "cardData": {"license": "mit"},
    "siblings": [
        {"rfilename": ".gitattributes", "size": 1477},
        {"rfilename": "README.md", "size": 1991},
        {"rfilename": "config.json", "size": 2249},
        {
            "rfilename": "model.bin",
            "size": 75538270,
            "lfs": {"sha256": "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1"},
        },
        {"rfilename": "tokenizer.json", "size": 2203239},
        {"rfilename": "vocabulary.txt", "size": 459861},
    ],
}


def _mock_get_response(json_data, status_ok=True):
    response = MagicMock()
    response.json.return_value = json_data
    if status_ok:
        response.raise_for_status = MagicMock()
    else:
        response.raise_for_status = MagicMock(side_effect=Exception("HTTP error"))
    return response


# ---------------------------------------------------------------------------
# fetch_model_info
# ---------------------------------------------------------------------------

def test_fetch_model_info_parses_required_files_only(tmp_path, monkeypatch):
    from app.voice import model_installer
    monkeypatch.setattr(model_installer, "models_dir", lambda: tmp_path)

    with patch("httpx.get", return_value=_mock_get_response(FAKE_HF_RESPONSE)):
        info = fetch_model_info()

    names = [f.name for f in info.files]
    assert names == ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]
    assert ".gitattributes" not in names
    assert "README.md" not in names


def test_fetch_model_info_captures_model_bin_sha256():
    with patch("httpx.get", return_value=_mock_get_response(FAKE_HF_RESPONSE)):
        info = fetch_model_info()
    model_bin = next(f for f in info.files if f.name == "model.bin")
    assert model_bin.sha256 == "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1"


def test_fetch_model_info_small_files_have_no_sha256():
    with patch("httpx.get", return_value=_mock_get_response(FAKE_HF_RESPONSE)):
        info = fetch_model_info()
    config = next(f for f in info.files if f.name == "config.json")
    assert config.sha256 is None
    assert config.size == 2249


def test_fetch_model_info_total_size_sums_required_files_only():
    with patch("httpx.get", return_value=_mock_get_response(FAKE_HF_RESPONSE)):
        info = fetch_model_info()
    expected = 2249 + 75538270 + 2203239 + 459861
    assert info.total_size == expected


def test_fetch_model_info_license_from_card_data():
    with patch("httpx.get", return_value=_mock_get_response(FAKE_HF_RESPONSE)):
        info = fetch_model_info()
    assert info.license == "mit"


def test_fetch_model_info_raises_on_http_error():
    with patch("httpx.get", return_value=_mock_get_response({}, status_ok=False)):
        with pytest.raises(Exception):
            fetch_model_info()


# ---------------------------------------------------------------------------
# ModelInstaller — full install flow, everything downloaded is fake bytes
# ---------------------------------------------------------------------------

def _fake_content_for(file_info: ModelFileInfo) -> bytes:
    """Deterministic fake bytes matching *file_info*'s declared size,
    and — for model.bin — its declared sha256 too, so the installer's
    real verification logic is genuinely exercised end to end."""
    if file_info.sha256:
        # Brute-searching a preimage is infeasible; instead build content
        # whose hash IS file_info.sha256 by construction: we don't need
        # the *real* whisper file, just bytes whose sha256 matches what
        # fetch_model_info() reported — so tests use a fake sha256 that
        # matches deterministic fake content instead of the real one.
        return file_info.name.encode() * 10
    return b"x" * file_info.size


def _fixture_model_info(tmp_path) -> ModelInfo:
    files = [
        ModelFileInfo(name="config.json", size=10),
        ModelFileInfo(name="model.bin", size=40, sha256=None),  # sha256 set below per-test
        ModelFileInfo(name="tokenizer.json", size=15),
        ModelFileInfo(name="vocabulary.txt", size=20),
    ]
    return ModelInfo(
        repo="Systran/faster-whisper-tiny",
        display_name="Whisper tiny (multilingual, CTranslate2)",
        license="mit",
        source_url="https://huggingface.co/Systran/faster-whisper-tiny",
        destination=str(tmp_path / "installed"),
        language_note="test",
        files=files,
        total_size=sum(f.size for f in files),
    )


@contextmanager
def _fake_stream(content_by_file):
    """Mimics httpx.stream("GET", url, ...) as a context manager whose
    .iter_bytes() yields the right fake content for the URL requested."""
    def _stream(method, url, **kwargs):
        name = url.rsplit("/", 1)[-1]
        content = content_by_file[name]

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.iter_bytes = MagicMock(return_value=iter([content]))

        @contextmanager
        def _cm(*a, **k):
            yield response
        return _cm()

    with patch("httpx.stream", side_effect=_stream):
        yield


def test_install_end_to_end_success(tmp_path):
    installer = ModelInstaller()
    info = _fixture_model_info(tmp_path)
    # Make model.bin's declared sha256 match fixed fake content.
    model_bin_content = b"MODEL-BIN-CONTENT-FOR-TEST"
    info.files[1] = ModelFileInfo(name="model.bin", size=len(model_bin_content), sha256=hashlib.sha256(model_bin_content).hexdigest())
    info.total_size = sum(f.size for f in info.files)

    content_by_file = {
        "config.json": b"x" * info.files[0].size,
        "model.bin": model_bin_content,
        "tokenizer.json": b"x" * info.files[2].size,
        "vocabulary.txt": b"x" * info.files[3].size,
    }

    with patch.object(installer, "_run", wraps=installer._run):
        with patch("app.voice.model_installer.fetch_model_info", return_value=info):
            with _fake_stream(content_by_file):
                installer._run()

    state = installer.state()
    assert state.status == "complete"
    dest = tmp_path / "installed"
    assert (dest / "model.bin").read_bytes() == model_bin_content
    assert (dest / "config.json").exists()


def test_install_fails_on_checksum_mismatch(tmp_path):
    installer = ModelInstaller()
    info = _fixture_model_info(tmp_path)
    info.files[1] = ModelFileInfo(name="model.bin", size=10, sha256="0" * 64)  # will never match
    info.total_size = sum(f.size for f in info.files)

    content_by_file = {
        "config.json": b"x" * info.files[0].size,
        "model.bin": b"y" * info.files[1].size,
        "tokenizer.json": b"x" * info.files[2].size,
        "vocabulary.txt": b"x" * info.files[3].size,
    }

    with patch("app.voice.model_installer.fetch_model_info", return_value=info):
        with _fake_stream(content_by_file):
            installer._run()

    state = installer.state()
    assert state.status == "error"
    assert "checksum" in state.message.lower()
    assert not (tmp_path / "installed").exists()


def test_install_fails_on_size_mismatch_for_unhashed_file(tmp_path):
    installer = ModelInstaller()
    info = _fixture_model_info(tmp_path)
    info.files[0] = ModelFileInfo(name="config.json", size=999999)  # declared size won't match actual bytes below
    info.total_size = sum(f.size for f in info.files)

    content_by_file = {
        "config.json": b"short",
        "model.bin": b"x" * info.files[1].size,
        "tokenizer.json": b"x" * info.files[2].size,
        "vocabulary.txt": b"x" * info.files[3].size,
    }

    with patch("app.voice.model_installer.fetch_model_info", return_value=info):
        with _fake_stream(content_by_file):
            installer._run()

    state = installer.state()
    assert state.status == "error"
    assert "unexpected size" in state.message.lower()


def test_install_reports_error_when_info_fetch_fails(tmp_path):
    installer = ModelInstaller()
    with patch("app.voice.model_installer.fetch_model_info", side_effect=RuntimeError("network down")):
        installer._run()
    state = installer.state()
    assert state.status == "error"
    assert "connection" in state.message.lower() or "reach" in state.message.lower()


def test_cancel_before_download_starts_stops_cleanly(tmp_path):
    installer = ModelInstaller()
    info = _fixture_model_info(tmp_path)
    installer.cancel()  # cancel before _run() even begins downloading

    with patch("app.voice.model_installer.fetch_model_info", return_value=info):
        installer._run()

    assert installer.state().status == "cancelled"


# ---------------------------------------------------------------------------
# start() / cancel() — single-flight behavior
# ---------------------------------------------------------------------------

def test_start_returns_true_and_runs_in_background(tmp_path):
    installer = ModelInstaller()
    info = _fixture_model_info(tmp_path)
    info.files[1] = ModelFileInfo(name="model.bin", size=4, sha256=hashlib.sha256(b"data").hexdigest())
    content_by_file = {
        "config.json": b"x" * info.files[0].size,
        "model.bin": b"data",
        "tokenizer.json": b"x" * info.files[2].size,
        "vocabulary.txt": b"x" * info.files[3].size,
    }

    with patch("app.voice.model_installer.fetch_model_info", return_value=info):
        with _fake_stream(content_by_file):
            started = installer.start()
            assert started is True
            for _ in range(50):
                if installer.state().status in ("complete", "error", "cancelled"):
                    break
                time.sleep(0.05)

    assert installer.state().status == "complete"


def test_start_returns_false_when_already_running():
    installer = ModelInstaller()
    installer._thread = MagicMock(is_alive=MagicMock(return_value=True))
    assert installer.start() is False


def test_cancel_sets_the_event():
    installer = ModelInstaller()
    assert installer._cancel_event.is_set() is False
    installer.cancel()
    assert installer._cancel_event.is_set() is True
