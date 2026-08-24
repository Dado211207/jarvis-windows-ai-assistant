"""Double-clap activation: the gates, and the boundary the feature lives in.

The detector itself is JavaScript and is tested for real — against a
real Chromium microphone playing synthesised claps — in
tests/test_clap_detection.py. This file covers the Python half: what the
server will and will not act on, and the structural guarantees that stop
this from quietly growing into the always-listening microphone
CLAUDE.md's Safety rules forbid.

Nothing here opens a microphone or plays audio.
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKLET = REPO_ROOT / "app" / "ui" / "static" / "clap-processor.js"
CONTROLLER = REPO_ROOT / "app" / "ui" / "static" / "clap-controller.js"


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


@pytest.fixture(autouse=True)
def never_really_speak():
    """No test in this file may reach a real speech engine.

    `activate()` calls `_speak_greeting()`, which is a no-op while
    spoken replies are off — and they are off by default, so nothing
    here *would* speak. That is incidental, not structural, and
    CLAUDE.md's Phase 3 rule asks for structural: on the Windows CI
    runner, which has no audio device, a preference file that came out
    the other way would put a real SAPI5 call in the middle of the test
    suite. Individual tests still patch `speak` themselves to assert on
    it; this is the floor underneath them.
    """
    from app.voice.tts import tts_service

    with patch.object(tts_service, "speak") as speak:
        speak.return_value = type("R", (), {"success": True, "message": ""})()
        yield speak


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
        "min_interval_seconds", "device_id", "tuning", "calibrated", "safe_bounds",
        "listener_state", "tray_label",
    }

    # The point of the assertion above, spelled out: settings and states,
    # never anything measured from the room.
    serialised = json.dumps(body).lower()
    for forbidden in ("sample", "audio", "waveform", "level", "rms", "transcript"):
        assert forbidden not in serialised


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

    for asset in (WORKLET, CONTROLLER):
        assert asset.exists()
        assert asset.parent.name == "static"
    assert 'app" / "ui" / "static"' in spec

    # And the page has to load the controller before app.js, or app.js's
    # first call into it finds nothing there.
    base = (REPO_ROOT / "app" / "ui" / "templates" / "base.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="/ui/static/([^"]+)"', base)
    assert scripts.index("clap-controller.js") < scripts.index("app.js")


def test_the_worklet_posts_nothing_but_the_bare_fact():
    """Two messages, and only two.

    Ordinary listening still emits one payload-free pair. Calibration
    adds three scalars — a time, a peak level, a gap — because telling
    somebody "that was too quiet" needs a number, and they never leave
    the page (see the two tests below). Anything richer than this list
    would be a channel for audio-derived data to escape the audio
    thread.
    """
    source = WORKLET.read_text(encoding="utf-8")
    posts = re.findall(r"postMessage\((.*?)\);", source, re.DOTALL)

    assert posts == [
        '{ type: "clap-onset", at: at, peak: peak, gap: gap }',
        '{ type: "clap-pair" }',
    ]


def test_the_onset_message_only_exists_during_calibration():
    """The extra scalars are guarded, not merely conventional. Ordinary
    listening cannot emit them however the detector is configured."""
    source = code_only(WORKLET.read_text(encoding="utf-8"))
    guard = source.index("if (this.calibrate)")
    onset = source.index('type: "clap-onset"')
    closing = source.index("}", onset)

    assert guard < onset < closing, "the onset message must sit inside the calibrate guard"


def test_calibration_measurements_are_never_sent_anywhere():
    """The raw onsets are measured in the page and thrown away.

    What *may* reach the server is the settings a person read and pressed
    Save on — three clamped numbers, which is a preference. The list of
    what was heard is not a preference, and no request carries it.
    """
    source = code_only((REPO_ROOT / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8"))

    for post in re.findall(r"API\.post\(([^;]*?)\)", source, re.DOTALL):
        assert not re.search(r"\bonsets?\b", post), f"a measured onset must not be posted: {post}"
        assert not re.search(r"\.peak\b", post), f"a measured level must not be posted: {post}"


def test_the_only_thing_the_listener_endpoint_accepts_is_a_known_state():
    """A status report that can carry an arbitrary string is a channel."""
    from app.voice import clap

    before = clap.listener_state()
    assert clap.report_listener_state("no-such-state") == before
    assert clap.report_listener_state("listening") == "listening"


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
    """The microphone is read, never played back. The listener now lives
    in the controller, which is the only place that opens a stream."""
    source = code_only(CONTROLLER.read_text(encoding="utf-8"))
    opener = source[source.index("async function start(") : source.index("async function reconcile")]

    assert "destination" not in opener
    assert "source.connect(node)" in opener


# ---------------------------------------------------------------------------
# The shared microphone choice
# ---------------------------------------------------------------------------

def test_no_microphone_is_chosen_by_default(clap_module):
    assert clap_module.device_id() == ""


def test_the_chosen_microphone_survives_and_is_reported(clap_module):
    clap_module.set_device_id("abc123")

    assert clap_module.device_id() == "abc123"
    assert clap_module.status()["device_id"] == "abc123"


def test_the_microphone_choice_is_shared_with_the_diagnostics_meter():
    """One preference key, not two. A second key is how the dropdown
    ended up moving nothing but its own test."""
    from app.core.preferences import STORABLE_KEYS

    assert "mic_device_id" in STORABLE_KEYS
    assert len([k for k in STORABLE_KEYS if k.endswith("device_id")]) == 1


# ---------------------------------------------------------------------------
# Calibrated tuning, and the bounds it can never leave
# ---------------------------------------------------------------------------

def test_nothing_is_calibrated_by_default(clap_module):
    assert clap_module.tuning() == {}
    assert clap_module.status()["calibrated"] is False


def test_calibration_only_changes_the_three_values_it_can_inform(clap_module):
    """The attack test and the sustained-sound cut-off are what separate
    a clap from speech. No calibration session may loosen them."""
    saved = clap_module.set_tuning({
        "absMin": 0.05, "minGap": 0.15, "maxGap": 0.6,
        "attackFall": 0.99, "maxTransient": 5.0, "floorRatio": 0.1, "refractory": 0.0,
    })

    assert set(saved) == {"absMin", "minGap", "maxGap"}
    detector = clap_module.detector_settings()
    profile = clap_module.SENSITIVITY_PROFILES[clap_module.sensitivity()]
    assert detector["attackFall"] == profile["attackFall"]
    assert detector["maxTransient"] == profile["maxTransient"]
    assert detector["floorRatio"] == profile["floorRatio"]


@pytest.mark.parametrize("key,proposed,expected", [
    ("absMin", 0.0, 0.008),      # a silent room cannot make it infinitely sensitive
    ("absMin", 9.9, 0.30),
    ("minGap", 0.0, 0.08),
    ("maxGap", 99.0, 1.20),
])
def test_an_unusable_value_is_clamped_rather_than_stored(clap_module, key, proposed, expected):
    assert clap_module.clamp_tuning({key: proposed})[key] == expected


@pytest.mark.parametrize("value", ["nonsense", None, float("nan"), float("inf")])
def test_a_value_that_is_not_a_number_is_dropped(clap_module, value):
    assert clap_module.clamp_tuning({"absMin": value}) == {}


def test_the_second_clap_always_has_somewhere_to_land(clap_module):
    """A window narrower than MIN_GAP_SPREAD is one nobody could hit."""
    cleaned = clap_module.clamp_tuning({"minGap": 0.30, "maxGap": 0.31})

    assert cleaned["maxGap"] - cleaned["minGap"] >= clap_module.MIN_GAP_SPREAD


def test_calibrated_values_override_the_profile_and_reset_restores_it(clap_module):
    profile_default = clap_module.SENSITIVITY_PROFILES["normal"]["absMin"]
    clap_module.set_tuning({"absMin": 0.11})
    assert clap_module.detector_settings()["absMin"] == 0.11

    clap_module.set_tuning(None)
    assert clap_module.tuning() == {}
    assert clap_module.detector_settings()["absMin"] == profile_default


def test_a_corrupt_tuning_preference_reads_as_not_calibrated(clap_module):
    from app.core import preferences

    preferences.store("clap_tuning", "{not json")
    assert clap_module.tuning() == {}


# ---------------------------------------------------------------------------
# What the listener is actually doing, and what the tray may claim
# ---------------------------------------------------------------------------

def test_a_stale_report_is_not_evidence_of_a_live_microphone(enabled, monkeypatch):
    """A closed or crashed page must not leave the tray saying "On"."""
    clock = {"now": 1000.0}
    monkeypatch.setattr("app.voice.clap.time.monotonic", lambda: clock["now"])

    enabled.report_listener_state("listening")
    assert enabled.listener_state() == "listening"

    clock["now"] += enabled.LISTENER_FRESH_SECONDS + 1
    assert enabled.listener_state() == "unknown"
    assert enabled.tray_label() == enabled.TRAY_LABELS["microphone-unavailable"]


def test_the_tray_never_says_on_without_a_fresh_live_report(enabled):
    assert enabled.tray_label() != enabled.TRAY_LABELS["listening"]

    enabled.report_listener_state("listening")
    assert enabled.tray_label() == enabled.TRAY_LABELS["listening"]


@pytest.mark.parametrize("reported,expected", [
    ("listening", "listening"),
    ("suspended", "suspended"),
    ("calibrating", "calibrating"),
    ("microphone-unavailable", "microphone-unavailable"),
    ("starting", "suspended"),
    ("stopping", "suspended"),
    ("error", "microphone-unavailable"),
])
def test_every_tray_status_transition(enabled, reported, expected):
    enabled.report_listener_state(reported)

    assert enabled.tray_label() == enabled.TRAY_LABELS[expected]


def test_the_tray_says_off_when_the_feature_is_off(clap_module):
    clap_module.report_listener_state("listening")

    assert clap_module.tray_label() == clap_module.TRAY_LABELS["off"]


def test_privacy_mode_wins_over_a_listening_report(enabled):
    """Belt and braces: even if a page were still reporting a live
    microphone, the tray tells the truth about why it should not be."""
    from app.core.privacy import privacy_mode

    enabled.report_listener_state("listening")
    privacy_mode.set(True)
    try:
        assert enabled.tray_label() == enabled.TRAY_LABELS["privacy-blocked"]
    finally:
        privacy_mode.set(False)


def test_the_listener_endpoint_requires_the_session_token(client):
    from app.api.session import HEADER_NAME

    response = client.post(
        "/voice/clap/listener", json={"state": "listening"}, headers={HEADER_NAME: "wrong"},
    )

    assert response.status_code == 403


def test_the_listener_endpoint_reports_the_tray_label_back(client):
    client.post("/voice/clap/enabled", json={"enabled": True})

    body = client.post("/voice/clap/listener", json={"state": "listening"}).json()

    assert body["listener_state"] == "listening"
    assert body["tray_label"] == "Double-clap listening: On"


def test_the_page_re_sends_its_report_before_the_server_stops_believing_it():
    """Staleness cuts both ways.

    A report that is only sent on a *change* decays: a page that sits
    happily listening for half a minute would leave the tray saying
    "Microphone unavailable" while the microphone is plainly open. The
    heartbeat interval has to be comfortably inside
    LISTENER_FRESH_SECONDS for the staleness window to mean "this page is
    gone" rather than "this page is quiet".
    """
    from app.voice import clap

    source = code_only((REPO_ROOT / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8"))
    match = re.search(r"CLAP_HEARTBEAT_MS\s*=\s*(\d+)", source)
    assert match, "the listener report is no longer re-sent on a timer"

    interval = int(match.group(1)) / 1000.0
    assert interval < clap.LISTENER_FRESH_SECONDS / 2, (
        f"a {interval}s heartbeat is too slow for a {clap.LISTENER_FRESH_SECONDS}s freshness window"
    )
    assert "setInterval(() => sendClapListenerState(clapState()), CLAP_HEARTBEAT_MS)" in source


def test_the_heartbeat_stops_with_the_page():
    """It is the *absence* of a heartbeat that tells the tray a page has
    gone. One that outlived its document would keep a dead tab's "On"
    alive."""
    source = code_only((REPO_ROOT / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8"))
    handlers = re.findall(
        r'addEventListener\("pagehide",\s*\(\)\s*=>\s*\{(.*?)\}\)', source, re.DOTALL,
    )
    releasing = [body for body in handlers if "setQuitting()" in body]

    assert releasing, "nothing releases the clap listener when the page goes away"
    assert any("stopClapHeartbeat()" in body for body in releasing), (
        "a heartbeat that outlives its document keeps a dead tab's \"On\" alive"
    )


def test_the_settings_endpoint_saves_a_device_and_a_tuning(client):
    body = client.post(
        "/voice/clap/settings", json={"device_id": "mic-7", "tuning": {"absMin": 0.05}},
    ).json()

    assert body["device_id"] == "mic-7"
    assert body["tuning"] == {"absMin": 0.05}
    assert body["calibrated"] is True
    assert body["detector"]["absMin"] == 0.05


# ---------------------------------------------------------------------------
# The packaged build detects exactly as the source checkout does
# ---------------------------------------------------------------------------

def test_the_worklet_defaults_match_the_servers_normal_profile():
    """A page that cannot reach `GET /voice/clap` must still detect the
    same way.

    `detector_settings()` is served to the page precisely so these numbers
    live in one place, but the worklet carries its own DEFAULTS for the
    case where the fetch fails — `startClapCalibration` swallows that
    error and falls back to them by design. If the two drift, a machine
    with a slow or failing settings request gets a *different detector*
    from every other machine, and nothing would report it.
    """
    from app.voice import clap

    source = WORKLET.read_text(encoding="utf-8")
    block = re.search(r"const DEFAULTS = \{(.*?)\n\};", source, re.DOTALL)
    assert block, "clap-processor.js no longer declares a DEFAULTS block"

    worklet_defaults = {
        name: float(value)
        for name, value in re.findall(r"^\s*(\w+):\s*([0-9.]+),", block.group(1), re.M)
    }
    expected = clap.SENSITIVITY_PROFILES[clap.DEFAULT_SENSITIVITY]

    assert worklet_defaults == pytest.approx(expected), (
        "the worklet's fallback detector and the server's default profile "
        f"disagree: worklet={worklet_defaults} server={expected}"
    )


def test_the_packaged_build_ships_the_same_detector_files():
    """The installed application must run this exact detector.

    The packaged build has no source tree: `app/ui/static` is bundled by
    the PyInstaller spec, and if either clap file stopped being included
    the packaged listener would be a different program from the one every
    test in this repository exercises.
    """
    spec = (REPO_ROOT / "packaging" / "jarvis.spec").read_text(encoding="utf-8")
    assert '"app" / "ui" / "static"' in spec.replace("'", '"'), (
        "the PyInstaller spec no longer bundles app/ui/static"
    )
    for required in ("clap-processor.js", "clap-controller.js"):
        assert (REPO_ROOT / "app" / "ui" / "static" / required).is_file(), (
            f"{required} is missing from the directory the spec bundles"
        )
