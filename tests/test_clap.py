"""Double-clap activation: the gates, and the boundary the feature lives in.

The detector itself is JavaScript and is tested for real — against a
real Chromium microphone playing synthesised claps — in
tests/test_clap_detection.py. This file covers the Python half: what the
server will and will not act on, and the structural guarantees that stop
this from quietly growing into the always-listening microphone
CLAUDE.md's Safety rules forbid.

Nothing here opens a microphone or plays audio.
"""

import re
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKLET = REPO_ROOT / "app" / "ui" / "static" / "clap-processor.js"


def code_only(source: str) -> str:
    """JavaScript with its comments removed.

    The structural assertions below are about what the code *does*, and
    the comments in both files necessarily name the things they explain
    why the code avoids ("not connected to the destination", "rather than
    an AnalyserNode"). Scanning the prose would make an accurate comment
    fail the test that the comment is describing.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", without_blocks, flags=re.MULTILINE)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app
    from app.voice import clap
    from tests.conftest import prime_session

    clap.reset_for_tests()
    with TestClient(jarvis_app, raise_server_exceptions=True) as test_client:
        yield prime_session(test_client)
    clap.reset_for_tests()


@pytest.fixture
def clap_module():
    from app.voice import clap

    clap.reset_for_tests()
    yield clap
    clap.reset_for_tests()


@pytest.fixture
def enabled(clap_module):
    clap_module.set_enabled(True)
    return clap_module


# ---------------------------------------------------------------------------
# Off until somebody turns it on
# ---------------------------------------------------------------------------

def test_disabled_by_default(clap_module):
    assert clap_module.enabled() is False
    assert clap_module.status()["enabled"] is False


def test_activation_refused_while_disabled(clap_module):
    with patch("app.launcher.attention.request") as request:
        outcome = clap_module.activate()

    assert outcome.accepted is False
    assert outcome.reason == "disabled"
    request.assert_not_called()


def test_turning_it_off_forgets_the_activation_history(enabled):
    with patch("app.launcher.attention.request", return_value=True):
        assert enabled.activate().accepted is True

    enabled.set_enabled(False)

    assert enabled.status()["seconds_since_activation"] is None


# ---------------------------------------------------------------------------
# Privacy mode. app/core/privacy.py's own docstring said any future
# listener must check it; this is that listener.
# ---------------------------------------------------------------------------

def test_privacy_mode_refuses_activation(enabled):
    from app.core.privacy import privacy_mode

    privacy_mode.set(True)
    try:
        with patch("app.launcher.attention.request") as request:
            outcome = enabled.activate()
    finally:
        privacy_mode.set(False)

    assert outcome.accepted is False
    assert outcome.reason == "privacy_mode"
    request.assert_not_called()


def test_privacy_mode_is_reported_so_the_page_can_stop_listening(enabled):
    from app.core.privacy import privacy_mode

    privacy_mode.set(True)
    try:
        assert enabled.status()["privacy_blocking"] is True
    finally:
        privacy_mode.set(False)


def test_a_privacy_read_that_fails_refuses_rather_than_activates(enabled):
    """Conservative direction on purpose: an unreadable privacy setting
    must not be treated as "privacy is off"."""
    with patch("app.core.privacy.privacy_mode") as broken:
        type(broken).active = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        with patch("app.launcher.attention.request") as request:
            outcome = enabled.activate()

    assert outcome.accepted is False
    assert outcome.reason == "privacy_mode"
    request.assert_not_called()


# ---------------------------------------------------------------------------
# The refractory interval
# ---------------------------------------------------------------------------

def test_a_second_activation_within_the_interval_is_refused(enabled):
    with patch("app.launcher.attention.request", return_value=True) as request:
        first = enabled.activate()
        second = enabled.activate()

    assert first.accepted is True
    assert second.accepted is False
    assert second.reason == "too_soon"
    assert request.call_count == 1


def test_activation_is_allowed_again_once_the_interval_passes(enabled, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr("app.voice.clap.time.monotonic", lambda: clock["now"])

    with patch("app.launcher.attention.request", return_value=True) as request:
        assert enabled.activate().accepted is True
        clock["now"] += enabled.MIN_INTERVAL_SECONDS + 0.1
        assert enabled.activate().accepted is True

    assert request.call_count == 2


# ---------------------------------------------------------------------------
# What an activation is allowed to do
# ---------------------------------------------------------------------------

def test_activation_only_asks_the_window_to_show(enabled):
    """The whole action. attention.request() writes a marker file whose
    existence is the entire message — there is no field in it that could
    name a command."""
    with patch("app.launcher.attention.request", return_value=True) as request:
        outcome = enabled.activate()

    assert outcome.accepted is True
    assert outcome.window_shown is True
    request.assert_called_once_with()


def test_activation_survives_a_window_signal_that_fails(enabled):
    with patch("app.launcher.attention.request", side_effect=OSError("no disk")):
        outcome = enabled.activate()

    assert outcome.accepted is True
    assert outcome.window_shown is False


def test_the_greeting_is_not_spoken_when_replies_are_not_spoken(enabled):
    from app.voice.tts import tts_service

    with patch.object(type(tts_service), "output_enabled", property(lambda self: False)):
        with patch.object(tts_service, "speak") as speak:
            with patch("app.launcher.attention.request", return_value=True):
                outcome = enabled.activate()

    assert outcome.accepted is True
    assert outcome.greeted is False
    speak.assert_not_called()


def test_the_greeting_is_spoken_when_replies_are_spoken(enabled):
    from app.voice.tts import tts_service

    with patch.object(type(tts_service), "output_enabled", property(lambda self: True)):
        with patch.object(tts_service, "speak") as speak:
            speak.return_value = type("R", (), {"success": True, "message": ""})()
            with patch("app.launcher.attention.request", return_value=True):
                outcome = enabled.activate()

    assert outcome.greeted is True
    speak.assert_called_once_with(enabled.DEFAULT_GREETING)


def test_the_greeting_can_be_switched_off_on_its_own(enabled):
    from app.voice.tts import tts_service

    enabled.set_greet_enabled(False)
    with patch.object(type(tts_service), "output_enabled", property(lambda self: True)):
        with patch.object(tts_service, "speak") as speak:
            with patch("app.launcher.attention.request", return_value=True):
                outcome = enabled.activate()

    assert outcome.accepted is True
    assert outcome.greeted is False
    speak.assert_not_called()


def test_a_speech_failure_does_not_fail_the_activation(enabled):
    from app.voice.tts import tts_service

    with patch.object(type(tts_service), "output_enabled", property(lambda self: True)):
        with patch.object(tts_service, "speak", side_effect=RuntimeError("no audio device")):
            with patch("app.launcher.attention.request", return_value=True):
                outcome = enabled.activate()

    assert outcome.accepted is True
    assert outcome.window_shown is True
    assert outcome.greeted is False


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_sensitivity_falls_back_to_normal_for_an_unknown_value(clap_module):
    assert clap_module.set_sensitivity("ludicrous") == "normal"
    assert clap_module.sensitivity() == "normal"


@pytest.mark.parametrize("name", ["low", "normal", "high"])
def test_every_sensitivity_profile_carries_a_complete_tuning(clap_module, name):
    clap_module.set_sensitivity(name)
    detector = clap_module.detector_settings()

    assert set(detector) == {
        "floorRatio", "absMin", "attackFall", "maxTransient",
        "minGap", "maxGap", "refractory",
    }
    assert all(isinstance(v, float) and v > 0 for v in detector.values())


def test_sensitivity_profiles_are_ordered_by_how_loud_a_clap_must_be(clap_module):
    profiles = clap_module.SENSITIVITY_PROFILES
    assert profiles["high"]["absMin"] < profiles["normal"]["absMin"] < profiles["low"]["absMin"]
    assert profiles["high"]["floorRatio"] < profiles["normal"]["floorRatio"] < profiles["low"]["floorRatio"]


def test_the_greeting_is_length_limited(clap_module):
    saved = clap_module.set_greeting("x" * 500)
    assert len(saved) <= clap_module.MAX_GREETING_CHARS


def test_clearing_the_greeting_restores_the_default_rather_than_silence(clap_module):
    """`preferences.store()` treats a blank value as "unset", so an empty
    string cannot mean "say nothing" — that is what `clap_greet` is for.
    Asserted so the two controls cannot silently collapse into one."""
    clap_module.set_greeting("")
    assert clap_module.greeting() == clap_module.DEFAULT_GREETING
    assert clap_module.greet_enabled() is True


def test_settings_survive_a_reload(clap_module):
    clap_module.set_enabled(True)
    clap_module.set_sensitivity("high")
    clap_module.set_greeting("Good evening.")
    clap_module.set_greet_enabled(False)

    assert clap_module.enabled() is True
    assert clap_module.sensitivity() == "high"
    assert clap_module.greeting() == "Good evening."
    assert clap_module.greet_enabled() is False


def test_clap_keys_are_on_the_preferences_allowlist():
    from app.core.preferences import STORABLE_KEYS

    for key in ("clap_enabled", "clap_sensitivity", "clap_greet", "clap_greeting"):
        assert key in STORABLE_KEYS


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/voice/clap/enabled",
    "/voice/clap/settings",
    "/voice/clap/activate",
])
def test_every_clap_mutation_requires_the_session_token(client, path):
    from app.api.session import HEADER_NAME

    response = client.post(path, json={"enabled": True}, headers={HEADER_NAME: "wrong"})

    assert response.status_code == 403


def test_activate_takes_no_request_body(client):
    """A clap has nothing to say. Extra fields are ignored rather than
    accepted into anything — asserted so a future field cannot be added
    without this test being deliberately changed."""
    from app.api import voice_routes

    fields = set(voice_routes.ClapActivateResponse.model_fields)
    assert fields == {"accepted", "reason", "window_shown", "greeted", "message"}

    signature = voice_routes.clap_activate.__code__.co_varnames[
        : voice_routes.clap_activate.__code__.co_argcount
    ]
    assert signature == ()


def test_status_reports_no_audio_and_no_levels(client):
    body = client.get("/voice/clap").json()

    assert set(body) == {
        "enabled", "sensitivity", "sensitivities", "greet", "greeting", "detector",
        "privacy_blocking", "activations", "last_reason", "seconds_since_activation",
        "min_interval_seconds",
    }


def test_enabling_and_disabling_through_the_api(client):
    assert client.post("/voice/clap/enabled", json={"enabled": True}).json()["enabled"] is True
    assert client.post("/voice/clap/enabled", json={"enabled": False}).json()["enabled"] is False


def test_settings_endpoint_saves_each_field_independently(client):
    client.post("/voice/clap/settings", json={"sensitivity": "high"})
    body = client.post("/voice/clap/settings", json={"greeting": "Evening."}).json()

    assert body["sensitivity"] == "high"
    assert body["greeting"] == "Evening."


def test_activate_through_the_api_refuses_while_disabled(client):
    with patch("app.launcher.attention.request") as request:
        body = client.post("/voice/clap/activate", json={}).json()

    assert body["accepted"] is False
    assert body["reason"] == "disabled"
    assert "switched off" in body["message"].lower()
    request.assert_not_called()


def test_activate_through_the_api_shows_the_window(client):
    client.post("/voice/clap/enabled", json={"enabled": True})
    with patch("app.launcher.attention.request", return_value=True) as request:
        body = client.post("/voice/clap/activate", json={}).json()

    assert body["accepted"] is True
    assert body["window_shown"] is True
    request.assert_called_once()


# ---------------------------------------------------------------------------
# The worklet, as source. These are the structural guarantees — the
# behavioural ones are in tests/test_clap_detection.py, against a real
# browser.
# ---------------------------------------------------------------------------

def test_the_worklet_is_bundled_with_the_application():
    """It lives in app/ui/static, which packaging/jarvis.spec ships whole.
    A detector that is not in the .exe is a feature that works in
    development and nowhere else."""
    spec = (REPO_ROOT / "packaging" / "jarvis.spec").read_text(encoding="utf-8")

    assert WORKLET.exists()
    assert WORKLET.parent.name == "static"
    assert 'app" / "ui" / "static"' in spec


def test_the_worklet_posts_nothing_but_the_bare_fact():
    """One message, no payload. Anything richer would be a channel for
    audio-derived data to leave the audio thread."""
    source = WORKLET.read_text(encoding="utf-8")
    posts = re.findall(r"postMessage\((.*?)\);", source, re.DOTALL)

    assert posts == ['{ type: "clap-pair" }']


def test_the_worklet_never_reads_frequency_content():
    """RMS and peak amplitude only. An FFT is the first step towards
    recognising *what* a sound was, which is exactly the line this
    feature does not cross."""
    source = code_only(WORKLET.read_text(encoding="utf-8"))

    for forbidden in ("fft", "FFT", "getByteFrequencyData", "getFloatFrequencyData",
                      "AnalyserNode", "MediaRecorder", "fetch(", "XMLHttpRequest",
                      "WebSocket", "localStorage", "IndexedDB"):
        assert forbidden not in source, f"{forbidden} has no business in the clap detector"


def test_the_worklet_keeps_no_audio_buffer():
    """Six scalars of state, all overwritten every block. A test that
    fails when somebody adds an array is the point."""
    source = code_only(WORKLET.read_text(encoding="utf-8"))

    for forbidden in ("new Float32Array", "new Array", "push(", "slice(", "concat(", ".set("):
        assert forbidden not in source, f"{forbidden} suggests audio is being retained"


def test_the_page_never_sends_audio_to_the_activation_endpoint():
    """The browser half posts an empty object. Asserted here because the
    endpoint's contract only holds if nobody starts filling it in."""
    source = (REPO_ROOT / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'API.post("/voice/clap/activate", {})' in source


def test_the_clap_stream_is_never_connected_to_the_speakers():
    source = (REPO_ROOT / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    listener = code_only(
        source[source.index("async function startClapListener") : source.index("function stopClapListener")]
    )

    assert "destination" not in listener
    assert "source.connect(clapNode)" in listener
