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


def test_fake_adapter_model_status_matches_availability():
    assert FakeSTTAdapter(available=True).model_status()[0] is True
    assert FakeSTTAdapter(available=False).model_status()[0] is False


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


def test_service_model_status_delegates_to_the_active_adapter():
    service = STTService()
    service.set_adapter_override(FakeSTTAdapter(available=True))
    try:
        assert service.model_status()[0] is True
    finally:
        service.set_adapter_override(None)


# --- FasterWhisperAdapter: contract tested via mocking, never real inference ---

def test_a_switched_off_feature_says_so_actionably():
    """The on/off switch is checked by the service, not by an adapter:
    whether the feature is offered is policy about the product, not a
    property of a particular speech engine. Living in one adapter meant
    every other adapter silently ignored it.

    Asserts the reason is *actionable for a packaged-app user*, not that
    it contains one specific word: this message reaches the Voice page,
    where "set an env var" was never a step anyone using the installed
    .exe could take (see tests/test_no_developer_instructions_in_ui.py).
    """
    from app.core.preferences import store
    from app.voice.stt import stt_service

    store("stt_enabled", "false")
    available, reason = stt_service.is_available()

    assert available is False
    assert "turned off" in reason.lower()
    assert "voice page" in reason.lower()


def test_the_adapter_reports_only_on_the_engine_it_owns():
    """Its answer must not depend on the user's on/off choice — that is
    the separation the diagnostics panel relies on to say "the engine is
    fine, you have simply switched voice input off"."""
    from app.core.preferences import store

    adapter = FasterWhisperAdapter()
    store("stt_enabled", "false")
    off = adapter.is_available()
    store("stt_enabled", "true")
    on = adapter.is_available()

    assert off == on


def test_faster_whisper_adapter_unavailable_when_package_missing():
    adapter = FasterWhisperAdapter()
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_enabled = True
        with patch.dict("sys.modules", {"faster_whisper": None}):
            available, reason = adapter.is_available()
    assert available is False
    assert "speech engine" in reason.lower()
    assert "pip" not in reason.lower(), "a packaged-app user has no terminal to run pip in"


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


# --- FasterWhisperAdapter.model_status(): never loads the model, never downloads ---

def test_model_status_ready_when_local_path_exists(tmp_path):
    model_dir = tmp_path / "whisper-tiny"
    model_dir.mkdir()
    adapter = FasterWhisperAdapter()
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_model_path = str(model_dir)
        ready, detail = adapter.model_status()
    assert ready is True
    assert str(model_dir) in detail


def test_model_status_missing_when_local_path_does_not_exist(tmp_path):
    adapter = FasterWhisperAdapter()
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_model_path = str(tmp_path / "does-not-exist")
        ready, detail = adapter.model_status()
    assert ready is False
    assert "does not exist" in detail.lower()


def test_model_status_missing_but_will_download_when_opted_in():
    adapter = FasterWhisperAdapter()
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_model_path = ""
        mock_settings.jarvis_stt_allow_download = True
        ready, detail = adapter.model_status()
    assert ready is False
    assert "download" in detail.lower()


def test_model_status_missing_with_no_path_and_no_download_opt_in():
    adapter = FasterWhisperAdapter()
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_model_path = ""
        mock_settings.jarvis_stt_allow_download = False
        ready, detail = adapter.model_status()
    assert ready is False
    assert detail


def test_model_status_never_loads_the_model():
    """Distinguishing feature from is_available(): must not trigger
    _get_model() (which would load or download a real model)."""
    adapter = FasterWhisperAdapter()
    adapter._get_model = MagicMock(side_effect=AssertionError("model_status() must not load the model"))
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_model_path = ""
        mock_settings.jarvis_stt_allow_download = False
        adapter.model_status()
    adapter._get_model.assert_not_called()


# --- FasterWhisperAdapter: the guided-install default directory (see
# app/voice/model_installer.py) is checked automatically, so a model
# installed from the setup page becomes usable with no .env edit and no
# restart ---

def test_model_status_ready_when_guided_install_dir_exists(tmp_path):
    adapter = FasterWhisperAdapter()
    adapter._guided_install_dir = MagicMock(return_value=tmp_path)
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_model_path = ""
        ready, detail = adapter.model_status()
    assert ready is True
    assert str(tmp_path) in detail


def test_model_status_explicit_path_takes_priority_over_guided_install_dir(tmp_path):
    """JARVIS_STT_MODEL_PATH is an explicit override — it must win even
    when it doesn't exist, rather than silently falling back."""
    adapter = FasterWhisperAdapter()
    adapter._guided_install_dir = MagicMock(return_value=tmp_path)
    missing = tmp_path / "explicit-path-does-not-exist"
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_model_path = str(missing)
        ready, detail = adapter.model_status()
    assert ready is False
    assert "does not exist" in detail.lower()


def test_get_model_uses_guided_install_dir_when_no_explicit_path(tmp_path, monkeypatch):
    import sys
    import types

    fake_module = types.ModuleType("faster_whisper")
    fake_model_cls = MagicMock()
    fake_module.WhisperModel = fake_model_cls

    adapter = FasterWhisperAdapter()
    adapter._guided_install_dir = MagicMock(return_value=tmp_path)
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_model_path = ""
        mock_settings.jarvis_stt_allow_download = False
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            adapter._get_model()

    assert fake_model_cls.call_args.args[0] == str(tmp_path)


def test_get_model_raises_when_neither_path_nor_guided_install_nor_download_available(tmp_path):
    import sys
    import types

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = MagicMock()

    adapter = FasterWhisperAdapter()
    adapter._guided_install_dir = MagicMock(return_value=tmp_path / "does-not-exist")
    with patch("app.config.settings") as mock_settings:
        mock_settings.jarvis_stt_model_path = ""
        mock_settings.jarvis_stt_allow_download = False
        mock_settings.jarvis_stt_model_size = "tiny"
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            with pytest.raises(RuntimeError, match="refusing to silently download"):
                adapter._get_model()
