"""Tests for app/voice/stt.py. No real audio, no real model, no
microphone — faster-whisper is not installed in this environment (and
must never be required for these tests to pass); everything here uses
FakeSTTAdapter or mocks the faster_whisper import boundary directly.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.voice.stt import (
    DEFAULT_TIMEOUT_SECONDS,
    FakeSTTAdapter,
    FasterWhisperAdapter,
    STTService,
)


# --- FakeSTTAdapter ---

def test_fake_adapter_is_available_by_default():
    adapter = FakeSTTAdapter()
    available, reason = adapter.is_available()
    assert available is True
    assert reason


def test_fake_adapter_can_be_forced_unavailable():
    adapter = FakeSTTAdapter(available=False)
    available, _reason = adapter.is_available()
    assert available is False


def test_fake_adapter_returns_configured_transcript():
    adapter = FakeSTTAdapter(transcript="turn on the lights")
    result = adapter.transcribe(Path("/tmp/whatever.webm"))
    assert result.success is True
    assert result.text == "turn on the lights"


def test_fake_adapter_records_calls_for_test_introspection():
    adapter = FakeSTTAdapter()
    adapter.transcribe(Path("/tmp/a.webm"), timeout_seconds=5.0)
    assert len(adapter.calls) == 1
    assert adapter.calls[0] == (Path("/tmp/a.webm"), 5.0)


# --- STTService resolution + override ---

def test_service_is_unavailable_by_default_without_faster_whisper():
    service = STTService()
    available, reason = service.is_available()
    # In this environment faster-whisper is genuinely not installed, and
    # even if it were, JARVIS_STT_ENABLED defaults to false.
    assert available is False
    assert reason


def test_service_transcribe_returns_the_unavailable_reason_as_message():
    service = STTService()
    result = service.transcribe(Path("/tmp/x.webm"))
    assert result.success is False
    assert result.text == ""
    assert result.message  # the honest "not installed"/"not configured" reason


def test_service_override_makes_a_fake_adapter_authoritative():
    service = STTService()
    fake = FakeSTTAdapter(transcript="hello jarvis")
    service.set_adapter_override(fake)
    try:
        available, _ = service.is_available()
        assert available is True
        result = service.transcribe(Path("/tmp/x.webm"))
        assert result.success is True
        assert result.text == "hello jarvis"
    finally:
        service.set_adapter_override(None)


def test_service_override_none_restores_the_real_adapter():
    service = STTService()
    service.set_adapter_override(FakeSTTAdapter())
    service.set_adapter_override(None)
    available, _reason = service.is_available()
    assert available is False  # back to the real (unconfigured) adapter


# --- FasterWhisperAdapter: contract tested via mocking, never real inference ---

def test_faster_whisper_adapter_unavailable_when_disabled_by_config():
    adapter = FasterWhisperAdapter()
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_enabled = False
        available, reason = adapter.is_available()
    assert available is False
    assert "disabled" in reason.lower()


def test_faster_whisper_adapter_unavailable_when_package_missing():
    adapter = FasterWhisperAdapter()
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_enabled = True
        with patch.dict("sys.modules", {"faster_whisper": None}):
            available, reason = adapter.is_available()
    assert available is False
    assert "not installed" in reason.lower()


def test_faster_whisper_adapter_refuses_to_download_without_explicit_opt_in():
    """The core 'no silent large-model download' guarantee: no local
    path and no explicit allow-download must refuse outright, never
    silently reach out to the network."""
    import sys
    import types

    fake_module = types.ModuleType("faster_whisper")
    fake_model_cls = MagicMock()
    fake_module.WhisperModel = fake_model_cls

    adapter = FasterWhisperAdapter()
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_model_path = ""
        mock_settings.jarvis_stt_allow_download = False
        mock_settings.jarvis_stt_model_size = "tiny"
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            with pytest.raises(RuntimeError, match="refusing to silently download"):
                adapter._get_model()

    fake_model_cls.assert_not_called()


def test_faster_whisper_adapter_uses_local_path_without_needing_allow_download():
    import sys
    import types

    fake_module = types.ModuleType("faster_whisper")
    fake_model_cls = MagicMock()
    fake_module.WhisperModel = fake_model_cls

    adapter = FasterWhisperAdapter()
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_model_path = "/opt/models/whisper-tiny-ct2"
        mock_settings.jarvis_stt_allow_download = False
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            adapter._get_model()

    fake_model_cls.assert_called_once()
    call_args = fake_model_cls.call_args
    assert call_args.args[0] == "/opt/models/whisper-tiny-ct2"


def test_faster_whisper_adapter_downloads_only_with_explicit_opt_in():
    import sys
    import types

    fake_module = types.ModuleType("faster_whisper")
    fake_model_cls = MagicMock()
    fake_module.WhisperModel = fake_model_cls

    adapter = FasterWhisperAdapter()
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_model_path = ""
        mock_settings.jarvis_stt_allow_download = True
        mock_settings.jarvis_stt_model_size = "tiny"
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            adapter._get_model()

    fake_model_cls.assert_called_once()
    assert fake_model_cls.call_args.args[0] == "tiny"


def test_faster_whisper_transcribe_enforces_a_bounded_timeout():
    import time

    adapter = FasterWhisperAdapter()
    adapter._get_model = MagicMock(side_effect=lambda: time.sleep(30) or MagicMock())

    start = time.monotonic()
    result = adapter.transcribe(Path("/tmp/x.webm"), timeout_seconds=0.2)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0
    assert result.success is False
    assert "did not finish" in result.message.lower()


def test_faster_whisper_transcribe_never_raises_on_model_error():
    adapter = FasterWhisperAdapter()
    adapter._get_model = MagicMock(side_effect=RuntimeError("model failed to load: /some/local/path"))
    result = adapter.transcribe(Path("/tmp/x.webm"))
    assert result.success is False
    assert "/some/local/path" not in result.message  # no raw internals leaked
