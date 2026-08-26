"""Regression coverage for persistence, privacy and cloud voice coordination."""

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


OPENAI_PREFERENCE_KEYS = {
    "openai_voice_key_configured",
    "openai_tts_model",
    "openai_tts_voice",
    "openai_tts_speed",
    "openai_tts_instructions",
    "openai_tts_fallback",
}


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.api.server import app
    from tests.conftest import prime_session

    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield prime_session(test_client)


def test_all_openai_preferences_are_allowlisted_and_round_trip_after_reload(tmp_path):
    from app.core import preferences
    from app.voice import engines

    with patch.object(preferences, "config_dir", return_value=tmp_path):
        assert OPENAI_PREFERENCE_KEYS <= set(preferences.STORABLE_KEYS)
        assert engines.set_openai_key_configured(True) is True
        assert engines.set_openai_settings(
            "gpt-4o-mini-tts", "onyx", 1.15, "Original restrained voice.",
        )
        assert engines.set_openai_fallback_allowed(False) is False

        # Every getter re-reads disk, which is the packaged restart path.
        assert engines.openai_key_configured() is True
        assert engines.selected_openai_model() == "gpt-4o-mini-tts"
        assert engines.selected_openai_voice() == "onyx"
        assert engines.selected_openai_speed() == 1.15
        assert engines.selected_openai_instructions() == "Original restrained voice."
        assert engines.openai_fallback_allowed() is False

        raw = json.loads((tmp_path / preferences.PREFERENCES_FILENAME).read_text())
        assert OPENAI_PREFERENCE_KEYS <= set(raw)
        assert "openai_api_key" not in raw
        assert all("secret" not in str(value).lower() for value in raw.values())


def test_openai_multi_setting_write_is_atomic_on_failure(tmp_path, monkeypatch):
    from app.core import preferences

    monkeypatch.setattr(preferences, "config_dir", lambda: tmp_path)
    assert preferences.store("openai_tts_voice", "cedar")
    original = (tmp_path / preferences.PREFERENCES_FILENAME).read_text()

    def fail_replace(_self, _target):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", fail_replace)
    assert preferences.store_many({
        "openai_tts_voice": "onyx",
        "openai_tts_speed": "1.25",
    }) is False
    assert (tmp_path / preferences.PREFERENCES_FILENAME).read_text() == original


def test_failed_openai_persistence_is_never_reported_as_success(client):
    with patch("app.core.preferences.store_many", return_value=False):
        body = client.post("/voice/openai/settings", json={
            "model": "gpt-4o-mini-tts",
            "voice": "cedar",
            "speed": 1.0,
            "instructions": "calm",
        }).json()
    assert body["success"] is False

    with patch("app.core.preferences.store", return_value=False):
        body = client.post("/voice/openai/fallback", json={"allowed": False}).json()
        engine = client.post("/voice/engine", json={"engine": "openai"}).json()
    assert body["success"] is False
    assert engine["success"] is False


def test_failed_key_status_persistence_removes_the_credential(client):
    with patch("app.core.credentials.set_openai_key", return_value=True), \
         patch("app.core.credentials.clear_openai_key", return_value=True) as clear, \
         patch("app.voice.engines.set_openai_key_configured", return_value=None):
        body = client.post("/voice/openai/key", json={"api_key": "synthetic"}).json()
    assert body["success"] is False
    clear.assert_called_once()


def test_timed_out_credential_set_is_eventually_reconciled_to_absent(monkeypatch):
    from app.core import credentials
    from tests.test_credentials import _install_fake_keyring

    release = threading.Event()

    class SlowWrite:
        def __init__(self):
            self._store = {}
            self.deleted = threading.Event()

        def set_password(self, service, username, value):
            release.wait(2)
            self._store[(service, username)] = value

        def delete_password(self, service, username):
            self._store.pop((service, username), None)
            self.deleted.set()

        def get_password(self, service, username):
            return self._store.get((service, username))

    fake = _install_fake_keyring(monkeypatch, SlowWrite())
    monkeypatch.setattr(credentials, "TIMEOUT_SECONDS", 0.03)
    assert credentials.set_openai_key("late-secret") is False
    release.set()
    assert fake.deleted.wait(2), "the late set must be followed by reconciliation delete"
    assert fake._store == {}


def test_timeout_set_delete_interleaving_cannot_leave_an_orphan(monkeypatch):
    from app.core import credentials
    from tests.test_credentials import _install_fake_keyring

    entered = threading.Event()
    release = threading.Event()

    class Interleaved:
        def __init__(self):
            self._store = {}
            self.deleted = threading.Event()

        def set_password(self, service, username, value):
            entered.set()
            release.wait(2)
            self._store[(service, username)] = value

        def delete_password(self, service, username):
            self._store.pop((service, username), None)
            self.deleted.set()

    fake = _install_fake_keyring(monkeypatch, Interleaved())
    monkeypatch.setattr(credentials, "TIMEOUT_SECONDS", 0.03)
    assert credentials.set_openai_key("late-secret") is False
    assert entered.is_set()
    assert credentials.clear_openai_key() is False
    release.set()
    assert fake.deleted.wait(2), "the serialized delete must run after the late set"
    assert fake._store == {}


def test_privacy_activation_during_credential_read_causes_zero_http_calls():
    from app.core.privacy import privacy_mode
    from app.voice import engines, openai_tts

    credential_read = threading.Event()
    release = threading.Event()
    cancel = threading.Event()
    player = MagicMock()
    player.begin_utterance.return_value = cancel
    # `side_effect` is called with the call's own arguments, and the
    # product calls `abandon(cancel)` — so a bare `cancel.set` became
    # `Event.set(cancel)` and raised TypeError inside the worker thread.
    # The thread swallowed it, `result` stayed empty, and this test failed
    # on an empty list rather than on the privacy behaviour it exists to
    # prove. Present on codex/voice-hardening too.
    player.stop.side_effect = lambda *_a, **_k: cancel.set()
    player.abandon.side_effect = lambda *_a, **_k: cancel.set()

    def delayed_key():
        credential_read.set()
        release.wait(2)
        return "synthetic"

    result = []
    privacy_mode.set(False)
    with patch("app.voice.audio.player", player), \
         patch("app.core.credentials.get_openai_key", side_effect=delayed_key), \
         patch.object(openai_tts, "_client") as client_factory, \
         patch.object(engines, "openai_fallback_allowed", return_value=False):
        worker = threading.Thread(target=lambda: result.append(engines._speak_openai("reply")))
        worker.start()
        assert credential_read.wait(1)
        privacy_mode.set(True)
        release.set()
        worker.join(2)
    privacy_mode.set(False)

    assert result and result[0].started is False
    client_factory.assert_not_called()


def test_provider_rechecks_privacy_on_the_final_line_before_send(monkeypatch):
    from app.voice import openai_tts

    entered = threading.Event()
    release = threading.Event()
    allowed = {"value": True}
    stream = MagicMock()

    class Client:
        def __enter__(self):
            entered.set()
            release.wait(2)
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return stream

    monkeypatch.setattr(openai_tts, "_client", lambda _timeout: Client())
    caught = []

    def call():
        try:
            openai_tts.synthesise_wav(
                "reply", "synthetic", privacy_guard=lambda: allowed["value"],
            )
        except openai_tts.OpenAITTSError as exc:
            caught.append(exc)

    worker = threading.Thread(target=call)
    worker.start()
    assert entered.wait(1)
    allowed["value"] = False
    release.set()
    worker.join(2)

    assert caught and caught[0].category == openai_tts.PRIVACY
    stream.assert_not_called()


@pytest.mark.parametrize("provider", ["openai", "elevenlabs"])
def test_stop_during_pending_cloud_response_prevents_late_playback(provider):
    """Both paid providers reserve the Player before waiting on HTTP."""
    from app.voice import audio, engines, elevenlabs, openai_tts

    entered = threading.Event()
    release = threading.Event()
    cancel = threading.Event()
    player = MagicMock()
    player.begin_utterance.return_value = cancel
    player.stop.side_effect = cancel.set
    player.is_current.side_effect = lambda token: token is cancel and not token.is_set()
    player.play_wav_bytes_if_current.side_effect = (
        lambda _wav, token: token is cancel and not token.is_set()
    )

    def delayed_synthesis(*_args, **kwargs):
        assert kwargs["cancel"] is cancel
        entered.set()
        release.wait(2)
        return b"late-wav"

    result = []
    common = [
        patch("app.voice.audio.player", player),
        patch("app.core.privacy.privacy_mode._active", False),
    ]
    if provider == "openai":
        common.extend([
            patch("app.core.credentials.get_openai_key", return_value="synthetic"),
            patch.object(openai_tts, "synthesise_wav", side_effect=delayed_synthesis),
            patch.object(engines, "openai_fallback_allowed", return_value=False),
        ])
        speak = engines._speak_openai
    else:
        common.extend([
            patch("app.core.credentials.get_elevenlabs_key", return_value="synthetic"),
            patch.object(engines, "selected_cloud_voice_id", return_value="voice-id"),
            patch.object(elevenlabs, "synthesise_wav", side_effect=delayed_synthesis),
            patch.object(engines, "fallback_allowed", return_value=False),
        ])
        speak = engines._speak_elevenlabs

    with common[0], common[1], common[2], common[3], common[4]:
        worker = threading.Thread(target=lambda: result.append(speak("reply")))
        worker.start()
        assert entered.wait(1)
        audio.player.stop()
        release.set()
        worker.join(2)

    assert result and result[0].started is False
    player.play_wav_bytes_if_current.assert_called_once_with(b"late-wav", cancel)


def test_local_ab_endpoint_never_uses_selected_cloud_provider(client):
    from app.voice import engines

    local = engines.SpeakOutcome(True, engines.KOKORO, "Speaking.")
    with patch("app.voice.engines.speak_local", return_value=local) as speak_local, \
         patch("app.voice.engines.speak") as cloud_speak:
        body = client.post("/voice/test", json={}).json()
    assert body["success"] is True
    assert body["engine"] == engines.KOKORO
    speak_local.assert_called_once()
    cloud_speak.assert_not_called()


def test_fallback_disclosure_is_per_utterance_not_process_global():
    from app.voice import engines

    def cloud(text):
        return engines.SpeakOutcome(
            False,
            engines.OPENAI,
            text,
            fallback=engines.FallbackDisclosure(engines.OPENAI, text, text),
        )

    with patch.object(engines, "selected_engine", return_value=engines.OPENAI), \
         patch.object(engines, "_speak_openai", side_effect=cloud), \
         patch.object(
             engines, "speak_local",
             side_effect=lambda text, **_kwargs: engines.SpeakOutcome(
                 True, engines.WINDOWS, f"local {text}",
             ),
         ):
        first = engines.speak("first")
        second = engines.speak("second")

    assert first.fallback.reason == "first"
    assert second.fallback.reason == "second"
    assert first.fallback is not second.fallback


def test_chat_exposes_stop_during_pending_cloud_and_discloses_fallback():
    source = Path("app/ui/static/app.js").read_text(encoding="utf-8")
    assert "speechRequestPending = true" in source
    assert "speechClickPending" in source
    assert "if (!speechClickPending) return;" in source
    assert "paintSpeakButton(btn, true)" in source
    assert "speechRequestGeneration" in source
    assert "if (generation !== speechRequestGeneration) return;" in source
    # The rendering moved into the one shared handler, which is the chat
    # pass's requirement; the generation guard above it is this pass's.
    # Both are asserted, rather than the two inline branches that used to
    # duplicate the rendering in each caller.
    assert "function handleSpeechResponse(result, btn, clearOrdinaryStatus)" in source
    assert "if (result.fallback)" in source
    assert source.count("handleSpeechResponse(r, btn,") == 2


@pytest.mark.parametrize("path", [
    "/voice/status",
    "/voice/speaking",
    "/voice/engine-status",
    "/voice/cloud",
    "/voice/openai",
    "/voice/clap",
    "/voice/diagnostics",
])
def test_sensitive_voice_state_gets_require_the_session(path):
    from fastapi.testclient import TestClient
    from app.api.server import app

    with TestClient(app, raise_server_exceptions=True) as bare:
        assert bare.get(path).status_code == 403


def test_security_branch_canonical_host_guard_remains_an_integration_requirement():
    source = Path("app/api/server.py").read_text(encoding="utf-8")
    if "TrustedHostMiddleware" not in source:
        pytest.skip("canonical Host guard lands from the security branch during integration")
    assert "TrustedHostMiddleware" in source


def test_transport_deadline_trust_and_wav_guards(monkeypatch):
    from app.voice import audio, elevenlabs, openai_tts

    client = openai_tts._client(1.0)
    try:
        assert client._trust_env is False  # noqa: SLF001
    finally:
        client.close()

    factory = MagicMock()
    monkeypatch.setattr(openai_tts, "_client", factory)
    with pytest.raises(openai_tts.OpenAITTSError) as caught:
        openai_tts.synthesise_wav("reply", "synthetic", deadline=0)
    assert caught.value.category == openai_tts.TIMEOUT
    factory.assert_not_called()
    assert audio.valid_wav_bytes(b"RIFF" + b"x" * 40) is False
    assert openai_tts.MAX_AUDIO_BYTES == elevenlabs.MAX_AUDIO_BYTES == 8 * 1024 * 1024
    eleven_source = Path("app/voice/elevenlabs.py").read_text(encoding="utf-8")
    assert "trust_env=False" in eleven_source
    assert "future.result(timeout=remaining)" in eleven_source


def test_packaging_selftest_and_clean_install_cover_cloud_voice():
    spec = Path("packaging/jarvis.spec").read_text(encoding="utf-8")
    selftest = Path("app/launcher/selftest.py").read_text(encoding="utf-8")
    clean = Path("scripts/test_clean_install.py").read_text(encoding="utf-8")
    for name in ("httpx", "httpcore", "app.voice.openai_tts", "winsound"):
        assert name in spec
        assert name.split(".")[-1] in selftest
    assert 'client.get("/voice/openai")' in clean
    assert "AI-generated voice" in clean


def test_openai_credential_is_owned_and_cleanup_failure_is_fail_closed_marker():
    from app.core import ownership

    # `ownership.RUNTIME_OWNED` has never existed under that name; the
    # tuple is CREATED_AT_RUNTIME, so this raised AttributeError instead of
    # checking anything. Present on codex/voice-hardening too.
    keys = {item.key for item in ownership.CREATED_AT_RUNTIME}
    # `openai_credential` here, not `openai_voice_credential`: the release
    # pass made this manifest generate from credentials.OWNED_CREDENTIALS,
    # and the voice branch (which predates that) added a second, hardcoded
    # entry beside it. Two cleanup paths for one secret is how one of them
    # stops matching the other, so there is one registry and this is its key.
    assert "openai_credential" in keys
    report = ownership.RemovalReport()
    with patch("app.core.credentials.get_stored_api_key", return_value=""), \
         patch("app.core.credentials.get_elevenlabs_key", return_value=""), \
         patch("app.core.credentials.get_openai_key", return_value="voice-key"), \
         patch("app.core.credentials.clear_openai_key", return_value=False):
        ownership._remove_credentials(report)
    assert any("OpenAI voice API key" in item for item in report.failed)


def test_voice_copy_and_approval_speech_boundary():
    readme = Path("README.md").read_text(encoding="utf-8")
    help_page = Path("app/ui/templates/help.html").read_text(encoding="utf-8")
    voice_page = Path("app/ui/templates/voice.html").read_text(encoding="utf-8")
    js = Path("app/ui/static/app.js").read_text(encoding="utf-8")
    combined = readme + help_page + voice_page
    for phrase in (
        "Speech-to-text is local",
        "AI-generated",
        "requires internet access",
        "may incur cost",
        "opt-in background",
    ):
        assert phrase in combined
    approval = js.split("function addApprovalCard", 1)[1].split("\nfunction ", 1)[0]
    assert "speakReply" not in approval
    assert "makeSpeakButton" not in approval
