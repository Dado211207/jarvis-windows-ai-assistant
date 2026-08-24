"""The clap detector, run for real: a real Chromium, a real microphone
capture path, real audio.

Marked `browser` alongside tests/test_playwright_e2e.py and excluded from
the default run for the same reason. Run explicitly:

    pytest -m browser tests/test_clap_detection.py -v

**Why this is not a mocked test.** The thing worth knowing about a clap
detector is whether it fires on a clap and stays quiet on speech, and no
amount of asserting on a state machine's internals answers that.
Chromium can be handed a WAV file to present as a microphone
(`--use-file-for-fake-audio-capture`), which makes the whole path real
from the capture device down: the same `getUserMedia` constraints the
product asks for, the same AudioWorklet running on the same audio
thread, the same `/voice/clap/activate` call into the same server.

The audio is synthesised here rather than committed, so there is no
binary fixture to trust and every waveform is described by the code that
makes it. The clips are:

  two claps        two 12 ms-decay broadband bursts, 260 ms apart
  one clap         one of them
  mistimed claps   two, 1.6 s apart — outside the pairing window
  speech           4 Hz amplitude-modulated tone-plus-noise, louder than
                   the claps, with a syllable-rate envelope
  sustained tone   a 220 Hz hum at a level well over the threshold
  near silence     the noise floor alone

Only the first may produce an activation. The other five are the reason
the detector requires a sharp attack and a short decay rather than just
"loud".

Nothing here plays audio out of a speaker: the stream is read and never
connected to a destination, which tests/test_clap.py asserts separately.
"""

import math
import random
import struct
import time
import wave
from pathlib import Path

import pytest

# One shared in-process server with the rest of the browser suite, so
# there is one port and one `settings.jarvis_port` for the origin
# allowlist to agree with. `live_server` is a session fixture defined in
# tests/conftest.py and is requested by name below — importing it here
# would register a second copy that binds the same port and dies.
from tests.conftest import BROWSER_BASE_URL as BASE_URL

pytestmark = pytest.mark.browser

RATE = 48000
CLIP_SECONDS = 6.0
# Longer than the clip, so a detector that was going to fire has had the
# whole recording to do it in.
WAIT_SECONDS = 9.0


# ---------------------------------------------------------------------------
# The audio
# ---------------------------------------------------------------------------

def _samples(seconds: float) -> int:
    return int(RATE * seconds)


def _noise_floor(n: int, amplitude: float = 0.004, seed: int = 3):
    rng = random.Random(seed)
    return [rng.uniform(-1, 1) * amplitude for _ in range(n)]


def _clap(seconds: float = 0.15, seed: int = 1):
    """Near-instant attack, ~12 ms exponential decay, broadband."""
    rng = random.Random(seed)
    n = _samples(seconds)
    return [rng.uniform(-1, 1) * math.exp(-i / (RATE * 0.012)) * 0.95 for i in range(n)]


def _speech_like(seconds: float, seed: int = 7):
    """Loud, but with a syllable-rate envelope that ramps over tens of
    milliseconds instead of arriving in one block."""
    rng = random.Random(seed)
    out = []
    for i in range(_samples(seconds)):
        t = i / RATE
        envelope = 0.5 + 0.5 * math.sin(2 * math.pi * 4.0 * t)
        value = 0.6 * math.sin(2 * math.pi * 150 * t) + 0.4 * rng.uniform(-1, 1)
        out.append(value * envelope * 0.55)
    return out


def _hum(seconds: float, frequency: float = 220.0, amplitude: float = 0.18):
    return [
        amplitude * math.sin(2 * math.pi * frequency * i / RATE)
        for i in range(_samples(seconds))
    ]


def _mix(base, overlay, at_seconds: float):
    start = _samples(at_seconds)
    for i, value in enumerate(overlay):
        if start + i < len(base):
            base[start + i] += value
    return base


def _write_wav(path: Path, samples) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(b"".join(
            struct.pack("<h", max(-32767, min(32767, int(s * 32767)))) for s in samples
        ))
    return path


def build_clip(name: str, directory: Path) -> Path:
    total = _samples(CLIP_SECONDS)
    audio = _noise_floor(total)

    if name == "two_claps":
        audio = _mix(audio, _clap(seed=1), 2.00)
        audio = _mix(audio, _clap(seed=2), 2.26)
    elif name == "one_clap":
        audio = _mix(audio, _clap(seed=1), 2.00)
    elif name == "mistimed_claps":
        audio = _mix(audio, _clap(seed=1), 1.50)
        audio = _mix(audio, _clap(seed=2), 3.10)
    elif name == "speech":
        audio = _mix(audio, _speech_like(3.0), 1.50)
    elif name == "hum":
        audio = _mix(audio, _hum(3.0), 1.50)
    elif name == "near_silence":
        pass
    else:  # pragma: no cover - a typo in a parametrize list
        raise ValueError(f"unknown clip {name!r}")

    return _write_wav(directory / f"{name}.wav", audio)


# ---------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------

@pytest.fixture
def clap_browser(playwright_instance):
    """A Chromium whose microphone is a file. One per test, because the
    file is chosen at launch — but the Playwright driver is the shared
    one from conftest, since it cannot be opened twice in a session."""
    from tests.conftest import chromium_executable_path

    launched = []

    def launch(wav: Path):
        try:
            browser = playwright_instance.chromium.launch(
                executable_path=chromium_executable_path(),
                args=[
                    "--use-fake-device-for-media-stream",
                    "--use-fake-ui-for-media-stream",
                    f"--use-file-for-fake-audio-capture={wav}%noloop",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
        except Exception as exc:  # pragma: no cover - environment
            pytest.skip(f"chromium is not available ({exc})")
        launched.append(browser)
        return browser

    yield launch

    for browser in launched:
        browser.close()


@pytest.fixture(autouse=True)
def never_really_speak(monkeypatch):
    """No test in this file may reach a real speech engine — CLAUDE.md's
    Phase 3 rule. Spoken replies are off by default so nothing here
    would speak anyway; this makes that structural rather than
    incidental."""
    from app.voice.tts import tts_service

    monkeypatch.setattr(
        type(tts_service), "speak",
        lambda self, text: type("R", (), {"success": True, "message": ""})(),
    )


@pytest.fixture
def armed(monkeypatch):
    """Clap activation switched on, with the window signal intercepted.

    The server runs in this same interpreter, so patching the launcher's
    marker-file write here is enough to keep the test from touching the
    developer's real AppData directory.
    """
    from app.voice import clap

    shown = []
    monkeypatch.setattr("app.launcher.attention.request", lambda: shown.append(1) or True)
    clap.reset_for_tests()
    clap.set_enabled(True)
    yield shown
    clap.set_enabled(False)
    clap.reset_for_tests()


def _activation_count() -> int:
    from app.voice import clap

    return clap.status()["activations"]


def _run_clip(clap_browser, clip: Path) -> int:
    browser = clap_browser(clip)
    context = browser.new_context(permissions=["microphone"])
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    before = _activation_count()
    page.goto(f"{BASE_URL}/ui/voice", wait_until="networkidle")
    # Started by the page's own boot code, from the setting the server
    # reports — nothing here reaches in to start it. The clips all begin
    # with 1.5 s of nothing, which is the margin this has to come up in.
    page.wait_for_function("clapListening() === true", timeout=10000)

    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if _activation_count() > before:
            break
        page.wait_for_timeout(250)

    assert errors == [], f"page errors: {errors}"
    fired = _activation_count() - before
    context.close()
    return fired


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------

def test_two_claps_bring_jarvis_forward(live_server, clap_browser, armed, tmp_path):
    clip = build_clip("two_claps", tmp_path)

    assert _run_clip(clap_browser, clip) == 1
    assert armed, "the window was not asked to show"


@pytest.mark.parametrize("clip_name", [
    "one_clap",
    "mistimed_claps",
    "speech",
    "hum",
    "near_silence",
])
def test_nothing_else_activates_jarvis(live_server, clap_browser, armed, tmp_path, clip_name):
    clip = build_clip(clip_name, tmp_path)

    assert _run_clip(clap_browser, clip) == 0
    assert armed == [], "the window was asked to show by something that was not a double clap"


# ---------------------------------------------------------------------------
# One input, one activation — and the refractory period that guarantees it
# ---------------------------------------------------------------------------

def _count_over_whole_clip(clap_browser, clip: Path) -> int:
    """Total activations across the *entire* recording.

    `_run_clip` stops counting the instant the first activation lands,
    which answers "did it fire?" and cannot answer "did it fire once?".
    A detector that fired twice for one pair would satisfy `== 1` there
    and be caught here.
    """
    browser = clap_browser(clip)
    context = browser.new_context(permissions=["microphone"])
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    before = _activation_count()
    page.goto(f"{BASE_URL}/ui/voice", wait_until="networkidle")
    page.wait_for_function("clapListening() === true", timeout=10000)

    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        page.wait_for_timeout(250)

    assert errors == [], f"page errors: {errors}"
    fired = _activation_count() - before
    context.close()
    return fired


def test_one_pair_produces_exactly_one_activation(live_server, clap_browser, armed, tmp_path):
    """Observed for the whole clip, not until the first hit."""
    clip = build_clip("two_claps", tmp_path)

    assert _count_over_whole_clip(clap_browser, clip) == 1
    assert armed == [1], f"the window was asked to show {len(armed)} times for one pair"


def test_the_refractory_period_suppresses_a_second_pair_and_then_releases(
    live_server, clap_browser, armed, tmp_path,
):
    """Three pairs, two activations — and which two is the whole point.

    Pairs land at 2.00/2.26, 3.00/3.26 and 5.00/5.26 seconds. The first
    fires and mutes the detector for `refractory` (1.5 s) from 2.26, so
    the 3.00 pair falls inside the mute and must produce nothing. The
    5.00 pair is past 3.76 and must fire again — a cooldown that never
    released would be just as broken as one that never engaged, and a
    test that only counted "not twice" would pass against it.
    """
    total = _samples(CLIP_SECONDS)
    audio = _noise_floor(total)
    audio = _mix(audio, _clap(seed=1), 2.00)
    audio = _mix(audio, _clap(seed=2), 2.26)
    audio = _mix(audio, _clap(seed=3), 3.00)    # inside the refractory period
    audio = _mix(audio, _clap(seed=4), 3.26)
    audio = _mix(audio, _clap(seed=5), 5.00)    # after it
    audio = _mix(audio, _clap(seed=6), 5.26)
    clip = _write_wav(tmp_path / "three_pairs.wav", audio)

    assert _count_over_whole_clip(clap_browser, clip) == 2, (
        "expected the first and third pairs to activate and the second — "
        "inside the refractory period — to be suppressed"
    )
    assert len(armed) == 2


def test_the_audio_fixtures_are_deterministic(tmp_path):
    """Every clip is seeded, so two builds are byte-identical.

    Not a browser test and deliberately not marked as one: if the audio
    could drift between runs, every detection result in this file would
    be measuring a different recording, and "flaky" would be unfalsifiable.
    """
    import hashlib

    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()

    for name in ("two_claps", "one_clap", "mistimed_claps", "speech", "hum", "near_silence"):
        a = hashlib.sha256(build_clip(name, first).read_bytes()).hexdigest()
        b = hashlib.sha256(build_clip(name, second).read_bytes()).hexdigest()
        assert a == b, f"{name}.wav differs between two builds of the same fixture"


def test_the_listener_does_not_start_while_the_feature_is_off(live_server, clap_browser, tmp_path):
    """The same two claps, with the setting off. Nothing opens the
    microphone and nothing reaches the server."""
    from app.voice import clap

    clap.reset_for_tests()
    clap.set_enabled(False)
    clip = build_clip("two_claps", tmp_path)

    browser = clap_browser(clip)
    context = browser.new_context(permissions=["microphone"])
    page = context.new_page()
    page.goto(f"{BASE_URL}/ui/voice", wait_until="networkidle")
    page.wait_for_timeout(int(WAIT_SECONDS * 1000))

    assert page.evaluate("typeof clapListening === 'function' && clapListening()") is False
    assert clap.status()["activations"] == 0
    context.close()


def test_privacy_mode_stops_the_listener(live_server, clap_browser, armed, tmp_path):
    """Switched on, but privacy mode is on too. app/core/privacy.py's own
    docstring required this of any future listener."""
    from app.core.privacy import privacy_mode
    from app.voice import clap

    clip = build_clip("two_claps", tmp_path)
    privacy_mode.set(True)
    try:
        browser = clap_browser(clip)
        context = browser.new_context(permissions=["microphone"])
        page = context.new_page()
        page.goto(f"{BASE_URL}/ui/voice", wait_until="networkidle")
        page.wait_for_timeout(int(WAIT_SECONDS * 1000))

        assert page.evaluate("clapListening()") is False
        assert clap.status()["activations"] == 0
        assert armed == []
        context.close()
    finally:
        privacy_mode.set(False)


def test_switching_it_off_stops_the_microphone(live_server, clap_browser, armed, tmp_path):
    """The stream and the audio context must both be released — a
    listener that is "off" but still holding the microphone is the
    failure this feature must never have.

    tests/test_clap_controller.py asserts this against the real
    MediaStreamTrack and AudioContext objects; this is the same claim
    made through the controller's own answer, in the file that owns the
    end-to-end audio path.
    """
    clip = build_clip("near_silence", tmp_path)

    browser = clap_browser(clip)
    context = browser.new_context(permissions=["microphone"])
    page = context.new_page()
    page.goto(f"{BASE_URL}/ui/voice", wait_until="networkidle")
    page.wait_for_function("clapListening() === true", timeout=10000)

    page.click("#clap-toggle")
    page.wait_for_function("clapListening() === false", timeout=10000)

    assert page.evaluate("ClapController.state()") == "disabled"
    assert page.evaluate("ClapController.activeDeviceId()") == ""
    context.close()


# ---------------------------------------------------------------------------
# The attack test, at sample-exact block phases
#
# A clap's attack does not respect 128-sample boundaries. When it lands
# near the end of a block, that block carries a sliver of the attack and
# its RMS rises just above the quiet gate — measured at 0.01268 against a
# gate of 0.01225 — so `prevRms < threshold * attackFall` is false for the
# loud block that follows and the whole clap is swallowed. A first clap
# lost that way is a double clap that never happens.
#
# Live capture cannot test this: the phase depends on where the audio
# device happens to start, which is exactly what nothing controls. An
# OfflineAudioContext renders from sample 0 in aligned 128-sample quanta,
# so a clap placed at sample N lands at phase N % 128, every time.
#
# This drives the real app/ui/static/clap-processor.js. Nothing is mocked
# and no algorithm is reimplemented — a detector asserted against a Python
# copy of itself would prove nothing about the file that ships.
# ---------------------------------------------------------------------------

OFFLINE_DETECTOR = """
async (spec) => {
  const RATE = 48000;
  // The audio arrives as base64 16-bit PCM — byte-for-byte the samples
  // _write_wav() would have put in a WAV. Synthesising it in JavaScript
  // instead looked equivalent and was not: the defect this guards
  // against is a block whose RMS lands 0.00043 above a gate, and a
  // different pseudo-random sequence simply does not reach that edge.
  // The first version of this test did exactly that and passed against
  // the broken detector.
  const raw = atob(spec.pcm);
  const total = raw.length / 2;
  const ctx = new OfflineAudioContext(1, total, RATE);
  await ctx.audioWorklet.addModule('/ui/static/clap-processor.js');

  const buffer = ctx.createBuffer(1, total, RATE);
  const data = buffer.getChannelData(0);
  for (let i = 0; i < total; i++) {
    let v = (raw.charCodeAt(i * 2) | (raw.charCodeAt(i * 2 + 1) << 8));
    if (v >= 0x8000) v -= 0x10000;
    data[i] = v / 32767;
  }

  const node = new AudioWorkletNode(ctx, 'clap-processor',
    { processorOptions: { calibrate: true } });
  const onsets = [];
  let pairs = 0;
  node.port.onmessage = (e) => {
    const d = e.data;
    if (!d) return;
    if (d.type === 'clap-onset') onsets.push({ at: d.at, peak: d.peak, gap: d.gap });
    else if (d.type === 'clap-pair') pairs += 1;
  };

  // A worklet is only pulled when it reaches the destination. Silenced,
  // and offline anyway, so nothing is ever audible.
  const silence = ctx.createGain();
  silence.gain.value = 0;
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(node);
  node.connect(silence);
  silence.connect(ctx.destination);
  source.start();

  await ctx.startRendering();
  await new Promise((r) => setTimeout(r, 150));   // let queued messages drain
  return { onsets: onsets, pairs: pairs };
}
"""


def _pcm16_base64(audio) -> str:
    """Exactly what _write_wav() would write, as base64."""
    import base64

    raw = b"".join(
        struct.pack("<h", max(-32767, min(32767, int(s * 32767)))) for s in audio
    )
    return base64.b64encode(raw).decode("ascii")


def _offline_clip(clap_samples, total_seconds=1.6):
    """Noise floor plus claps at exact sample positions."""
    audio = _noise_floor(_samples(total_seconds))
    for position, seed in clap_samples:
        audio = _mix(audio, _clap(seed=seed), position / RATE)
    return audio


def _render_offline(clap_browser, tmp_path, audio):
    browser = clap_browser(build_clip("near_silence", tmp_path))
    context = browser.new_context(permissions=["microphone"])
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{BASE_URL}/ui/voice", wait_until="networkidle")
    result = page.evaluate(OFFLINE_DETECTOR, {"pcm": _pcm16_base64(audio)})
    assert errors == [], f"page errors: {errors}"
    context.close()
    return result


@pytest.mark.parametrize("phase", [0, 62, 126, 127])
def test_a_clap_is_detected_whatever_block_phase_its_attack_lands_on(
    live_server, clap_browser, armed, tmp_path, phase,
):
    """Phases 62, 126 and 127 were missed entirely before this was fixed.

    48000 is a multiple of 128, so a clap at sample `48000 + phase`
    begins exactly `phase` samples into a render quantum. The second
    clap follows 0.26 s later, inside the 0.12-0.7 s pairing window.
    Phase 0 is the control: it always worked and must keep working.
    """
    first = _samples(1.0) + phase
    audio = _offline_clip([(first, 11), (first + _samples(0.26), 12)])
    result = _render_offline(clap_browser, tmp_path, audio)

    assert len(result["onsets"]) == 2, (
        f"block phase {phase}: the detector reported {len(result['onsets'])} onset(s), "
        f"not 2 — an attack straddling a block boundary was swallowed. "
        f"Got {result['onsets']}"
    )
    assert result["pairs"] == 1, f"block phase {phase}: {result['pairs']} pairs, expected 1"


def test_the_offline_harness_still_rejects_what_it_should(
    live_server, clap_browser, armed, tmp_path,
):
    """The phase fix must not have bought detection with false positives.

    One clap alone, and two claps too far apart, at the same awkward
    phase that exposed the defect.
    """
    single = _render_offline(
        clap_browser, tmp_path, _offline_clip([(_samples(1.0) + 62, 11)]))
    assert single["pairs"] == 0, "a single clap activated"
    assert len(single["onsets"]) == 1, "a single clap did not produce exactly one onset"

    mistimed = _render_offline(clap_browser, tmp_path, _offline_clip(
        [(_samples(0.3) + 62, 11), (_samples(1.2) + 62, 12)]))
    assert mistimed["pairs"] == 0, "two claps 0.9 s apart activated"


def test_speech_and_a_hum_produce_no_onset_at_an_awkward_phase(
    live_server, clap_browser, armed, tmp_path,
):
    """The attack test was loosened to catch straddled claps. This is the
    other half of that trade: the sounds it must still refuse."""
    for name, overlay in (("speech", _speech_like(1.0)), ("hum", _hum(1.0))):
        audio = _noise_floor(_samples(1.6))
        audio = _mix(audio, overlay, (_samples(0.3) + 62) / RATE)
        result = _render_offline(clap_browser, tmp_path, audio)
        assert result["pairs"] == 0, f"{name} activated"
        assert result["onsets"] == [], f"{name} produced onsets: {result['onsets']}"
