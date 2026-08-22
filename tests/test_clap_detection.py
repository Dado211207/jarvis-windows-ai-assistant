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
    failure this feature must never have."""
    clip = build_clip("near_silence", tmp_path)

    browser = clap_browser(clip)
    context = browser.new_context(permissions=["microphone"])
    page = context.new_page()
    page.goto(f"{BASE_URL}/ui/voice", wait_until="networkidle")
    page.wait_for_function("clapListening() === true", timeout=10000)

    page.click("#clap-toggle")
    page.wait_for_function("clapListening() === false", timeout=10000)

    assert page.evaluate("clapStream === null && clapContext === null && clapNode === null")
    context.close()
