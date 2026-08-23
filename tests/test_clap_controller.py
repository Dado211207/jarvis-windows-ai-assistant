"""The clap listener's lifecycle, run for real in Chromium.

Marked `browser` alongside tests/test_clap_detection.py and
tests/test_playwright_e2e.py, and excluded from the default run for the
same reason. Run explicitly:

    pytest -m browser tests/test_clap_controller.py -v

**Why none of this is a mocked test.** The defect this file exists to
catch was a microphone that kept running after privacy mode was switched
on. A boolean somewhere said "not listening" and the Windows
microphone-in-use indicator stayed lit, because the `MediaStreamTrack`
was never stopped. Asserting on that boolean would have passed against
the broken build.

So every assertion here is on the real objects the real page created.
An init script — installed before any of the page's own scripts run —
wraps `getUserMedia`, the `AudioContext` constructor, the
`AudioWorkletNode` constructor, `navigator.mediaDevices`' event
listeners, `setTimeout`/`clearTimeout` and `fetch`, keeping a reference
to everything the controller opens. A test can then ask the browser
questions no bookkeeping flag could answer:

  * how many `MediaStreamTrack`s opened by app/ui/static/clap-controller.js
    are still `readyState === "live"`,
  * how many `AudioContext`s it created are not `state === "closed"`,
  * how many `AudioWorkletNode`s it built have never had `disconnect()`
    called,
  * how many `devicechange` listeners it registered,
  * how many timers it scheduled that neither fired nor were cleared,
  * and exactly which constraints object it handed to `getUserMedia`.

Stack-frame attribution (`new Error().stack` contains
`clap-controller.js`) is what separates the controller's own resources
from the diagnostics level meter's and push-to-talk's, which share the
same page and the same APIs.

The microphone is a file, as in tests/test_clap_detection.py: near
silence for the lifecycle tests, so nothing spurious fires, and one
synthesised pair for the calibration test, which needs real onsets to
measure. The waveform primitives are imported from that module rather
than copied — there is one place in this suite that describes what a
clap sounds like.
"""

import json
import time
from pathlib import Path

import pytest

from tests.conftest import BROWSER_BASE_URL as BASE_URL
from tests.conftest import SESSION_TOKEN_HEADER

# One definition of the synthetic audio for the whole suite.
from tests.test_clap_detection import _clap, _mix, _noise_floor, _samples, _write_wav

pytestmark = pytest.mark.browser

# Long enough that a page which loads slowly still has silence to come up
# in, and that "wait and prove nothing restarted" waits are meaningful.
QUIET_SECONDS = 30.0

# The controller waits this long after the last suspension reason clears
# before reopening the microphone; anything that asserts "still not
# listening" has to outlast it to mean anything.
RESUME_DELAY_MS = 450


# ---------------------------------------------------------------------------
# The audio
# ---------------------------------------------------------------------------

def quiet_clip(directory: Path) -> Path:
    """Thirty seconds of noise floor. Nothing in it can be a clap."""
    return _write_wav(directory / "quiet.wav", _noise_floor(_samples(QUIET_SECONDS)))


def late_pair_clip(directory: Path) -> Path:
    """Silence, then one genuine pair at eight seconds.

    Late on purpose: the page has to load, the listener has to come up,
    and a person has to press Calibrate before the claps arrive, or the
    test would be measuring the ordinary listener instead of the
    calibration session.
    """
    audio = _noise_floor(_samples(QUIET_SECONDS))
    audio = _mix(audio, _clap(seed=11), 8.00)
    audio = _mix(audio, _clap(seed=12), 8.26)
    return _write_wav(directory / "late_pair.wav", audio)


# ---------------------------------------------------------------------------
# The instrumentation
# ---------------------------------------------------------------------------

INSTRUMENT = r"""
(() => {
  "use strict";

  window.__mic = { calls: [], streams: [] };
  window.__ctxs = [];
  window.__nodes = [];
  window.__deviceChangeListeners = 0;
  window.__clapTimers = new Map();
  window.__requests = [];

  // Which feature opened this resource. The diagnostics level meter and
  // push-to-talk use the same three APIs on the same page, so without
  // this every count would be a mix of three features.
  //
  // Deliberately loose — any frame mentioning "clap", which catches both
  // app/ui/static/clap-controller.js and a function named
  // startClapListener. That matters because this same file is run
  // against the *previous* commit to demonstrate the regressions (see
  // docs/double-clap-activation.md), and on that commit the listener
  // lived in app.js with no controller at all. Nothing else on the page
  // that opens a microphone has "clap" anywhere in its call stack.
  function fromController() {
    try {
      return /clap/i.test(String(new Error().stack));
    } catch (e) {
      return false;
    }
  }

  const md = navigator.mediaDevices;

  const realGum = md.getUserMedia.bind(md);
  md.getUserMedia = function (constraints) {
    const record = {
      constraints: JSON.parse(JSON.stringify(constraints || {})),
      fromController: fromController(),
      ok: null,
      error: "",
    };
    window.__mic.calls.push(record);
    return realGum(constraints).then(function (s) {
      record.ok = true;
      window.__mic.streams.push({ stream: s, fromController: record.fromController });
      return s;
    }, function (e) {
      record.ok = false;
      record.error = (e && e.name) || String(e);
      throw e;
    });
  };

  const realAdd = md.addEventListener.bind(md);
  const realRemove = md.removeEventListener.bind(md);
  md.addEventListener = function (type) {
    if (type === "devicechange") window.__deviceChangeListeners += 1;
    return realAdd.apply(md, arguments);
  };
  md.removeEventListener = function (type) {
    if (type === "devicechange") window.__deviceChangeListeners -= 1;
    return realRemove.apply(md, arguments);
  };

  window.AudioContext = new Proxy(window.AudioContext, {
    construct(target, args) {
      const c = Reflect.construct(target, args);
      window.__ctxs.push({ ctx: c, fromController: fromController() });
      return c;
    },
  });

  window.AudioWorkletNode = new Proxy(window.AudioWorkletNode, {
    construct(target, args) {
      const n = Reflect.construct(target, args);
      n.__disconnects = 0;
      const realDisconnect = n.disconnect.bind(n);
      n.disconnect = function () {
        n.__disconnects += 1;
        return realDisconnect.apply(n, arguments);
      };
      window.__nodes.push(n);
      return n;
    },
  });

  const realSetTimeout = window.setTimeout;
  const realClearTimeout = window.clearTimeout;
  window.setTimeout = function (fn, ms) {
    if (!fromController() || typeof fn !== "function") {
      return realSetTimeout.apply(window, arguments);
    }
    const rest = Array.prototype.slice.call(arguments, 2);
    let id = null;
    id = realSetTimeout.call(window, function () {
      const rec = window.__clapTimers.get(id);
      if (rec) rec.fired = true;
      return fn.apply(window, rest);
    }, ms);
    window.__clapTimers.set(id, { fired: false, cleared: false, ms: ms });
    return id;
  };
  window.clearTimeout = function (id) {
    const rec = window.__clapTimers.get(id);
    if (rec) rec.cleared = true;
    return realClearTimeout.call(window, id);
  };

  const realFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = (typeof input === "string") ? input : ((input && input.url) || "");
      const body = (init && typeof init.body === "string") ? init.body : "";
      window.__requests.push({ url: url, body: body });
    } catch (e) { /* never break a request to record it */ }
    return realFetch.apply(window, arguments);
  };

  // ── Questions the tests ask ───────────────────────────────────────────

  window.__liveClapTracks = function () {
    let n = 0;
    window.__mic.streams.forEach(function (s) {
      if (!s.fromController) return;
      s.stream.getTracks().forEach(function (t) { if (t.readyState === "live") n += 1; });
    });
    return n;
  };
  window.__endedClapTracks = function () {
    let n = 0;
    window.__mic.streams.forEach(function (s) {
      if (!s.fromController) return;
      s.stream.getTracks().forEach(function (t) { if (t.readyState === "ended") n += 1; });
    });
    return n;
  };
  window.__openClapContexts = function () {
    return window.__ctxs.filter(function (c) {
      return c.fromController && c.ctx.state !== "closed";
    }).length;
  };
  window.__connectedClapNodes = function () {
    return window.__nodes.filter(function (n) { return !n.__disconnects; }).length;
  };
  window.__clapGumCalls = function () {
    return window.__mic.calls.filter(function (c) { return c.fromController; });
  };
  // Per-stream, in the order the controller opened them, so a test can
  // name *which* stream had to be released rather than counting.
  window.__clapStreamStates = function () {
    return window.__mic.streams.filter(function (s) { return s.fromController; })
      .map(function (s) {
        return s.stream.getTracks().map(function (t) { return t.readyState; });
      });
  };
  window.__pendingClapTimers = function () {
    let n = 0;
    window.__clapTimers.forEach(function (rec) { if (!rec.fired && !rec.cleared) n += 1; });
    return n;
  };

  // Refuse the microphone to everything on the page except the clap
  // listener. That is how a test can fail the diagnostics level meter
  // without also failing the listener's own restart, which is the thing
  // being measured.
  window.__denyNonClapMicrophone = function () {
    const real = md.getUserMedia.bind(md);
    md.getUserMedia = function () {
      if (fromController()) return real.apply(md, arguments);
      const err = new Error("Permission denied");
      err.name = "NotAllowedError";
      return Promise.reject(err);
    };
  };

  // Make one microphone vanish from enumerateDevices without unplugging
  // anything — the software half of pulling a USB microphone out.
  window.__hideDevice = function (id) {
    const real = md.enumerateDevices.bind(md);
    md.enumerateDevices = function () {
      return real().then(function (list) {
        return list.filter(function (d) {
          return !(d.kind === "audioinput" && d.deviceId === id);
        });
      });
    };
  };
})();
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class Session:
    def __init__(self, page, errors):
        self.page = page
        self.errors = errors

    def js(self, expression):
        return self.page.evaluate(expression)

    def live(self):
        return self.page.evaluate("__liveClapTracks()")

    def ended(self):
        return self.page.evaluate("__endedClapTracks()")

    def contexts(self):
        return self.page.evaluate("__openClapContexts()")

    def nodes(self):
        return self.page.evaluate("__connectedClapNodes()")

    def gum(self):
        return self.page.evaluate("__clapGumCalls()")

    def streams(self):
        """Track readyStates per controller-opened stream, in order."""
        return self.page.evaluate("__clapStreamStates()")

    def timers(self):
        return self.page.evaluate("__pendingClapTimers()")

    def state(self):
        return self.page.evaluate("clapState()")

    def requests(self):
        return self.page.evaluate("__requests")

    def settled(self):
        """Every resource released — nothing live, open, connected or
        pending. The shape of "the microphone is genuinely off"."""
        return (self.live(), self.contexts(), self.nodes())


@pytest.fixture(autouse=True)
def never_really_speak(monkeypatch):
    """CLAUDE.md's Phase 3 rule: no test may reach a real speech engine.
    Nothing here would speak — spoken replies are off by default — which
    is exactly why this should be structural rather than incidental."""
    from app.voice.tts import tts_service

    monkeypatch.setattr(
        type(tts_service), "speak",
        lambda self, text: type("R", (), {"success": True, "message": ""})(),
    )


@pytest.fixture(autouse=True)
def armed(monkeypatch):
    """Clap activation on, privacy off, and the window signal intercepted.

    The server runs in this interpreter, so patching the launcher's
    marker-file write keeps the tests out of the developer's real AppData
    directory.
    """
    from app.core.privacy import privacy_mode
    from app.voice import clap

    shown = []
    monkeypatch.setattr("app.launcher.attention.request", lambda: shown.append(1) or True)
    clap.reset_for_tests()
    clap.set_enabled(True)
    privacy_mode.set(False)
    yield shown
    # The saved microphone and tuning need no cleanup here: conftest's
    # isolated_preferences gives every test its own preferences file, and
    # both are read from it on every call rather than cached.
    privacy_mode.set(False)
    clap.set_enabled(False)
    clap.reset_for_tests()


@pytest.fixture
def voice_page(playwright_instance, tmp_path, live_server):
    """A Voice page whose microphone is a file, instrumented before any of
    its own scripts run."""
    from tests.conftest import chromium_executable_path

    browsers = []

    def _open(wav=None, wait_for_listening=True):
        clip = wav or quiet_clip(tmp_path)
        try:
            browser = playwright_instance.chromium.launch(
                executable_path=chromium_executable_path(),
                args=[
                    "--use-fake-device-for-media-stream",
                    "--use-fake-ui-for-media-stream",
                    f"--use-file-for-fake-audio-capture={clip}%noloop",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
        except Exception as exc:  # pragma: no cover - environment
            pytest.skip(f"chromium is not available ({exc})")
        browsers.append(browser)
        context = browser.new_context(permissions=["microphone"])
        page = context.new_page()
        page.add_init_script(INSTRUMENT)
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{BASE_URL}/ui/voice", wait_until="networkidle")
        if wait_for_listening:
            page.wait_for_function("clapListening() === true", timeout=20000)
        return Session(page, errors)

    yield _open

    for browser in browsers:
        browser.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_command(text: str) -> dict:
    """Drive the product the way a chat command or the tray does — over
    the real endpoint, so the real WebSocket event is broadcast and the
    open page has to react to it."""
    import httpx

    with httpx.Client(base_url=BASE_URL, timeout=15.0) as client:
        client.get("/health")
        token = client.cookies.get("jarvis_session") or ""
        response = client.post(
            "/command", json={"command": text}, headers={SESSION_TOKEN_HEADER: token},
        )
        response.raise_for_status()
        return response.json()


def server_clap_status() -> dict:
    from app.voice import clap

    return clap.status()


def wait_for(session: Session, expression: str, timeout: float = 10.0) -> None:
    session.page.wait_for_function(expression, timeout=int(timeout * 1000))


def wait_for_server_state(session: Session, state: str, timeout: float = 12.0) -> None:
    """The listener report travels browser → server over a POST, so the
    server learns a moment after the page changes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_clap_status()["listener_state"] == state:
            return
        session.page.wait_for_timeout(200)
    pytest.fail(
        f"the server never saw listener state {state!r} "
        f"(it has {server_clap_status()['listener_state']!r})"
    )


def keys_at_any_depth(value):
    """Every key name anywhere in a decoded JSON body."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            found |= keys_at_any_depth(item)
    elif isinstance(value, list):
        for item in value:
            found |= keys_at_any_depth(item)
    return found


# ---------------------------------------------------------------------------
# §1 Privacy mode releases the microphone
# ---------------------------------------------------------------------------

def test_privacy_mode_stops_the_microphone_not_just_the_label(voice_page):
    """The regression test for the defect this pass exists to fix.

    Privacy mode is switched on the way a chat command or the tray
    switches it on — through POST /command, which broadcasts the event
    the open page listens for. What is asserted afterwards is not a flag:
    it is that every `MediaStreamTrack` the controller opened has stopped,
    every `AudioContext` it created is closed, and its worklet node has
    been disconnected.

    The first wait below is deliberately written against the
    instrumentation rather than against any product API, so it means the
    same thing on both commits: run against the previous build it times
    out with a live track, which is the defect, stated as a measurement.
    """
    session = voice_page()
    assert session.live() == 1, "the listener should be holding exactly one microphone track"
    assert session.contexts() == 1
    assert session.nodes() == 1

    run_command("privacy mode on")

    session.page.wait_for_function("__liveClapTracks() === 0", timeout=15000)
    assert session.contexts() == 0, "an AudioContext survived privacy mode"
    assert session.nodes() == 0, "the AudioWorklet node was never disconnected"
    assert session.ended() >= 1, "no track was ever stopped"
    assert session.state() == "privacy-blocked"
    assert session.errors == []


def test_privacy_mode_from_the_settings_page_stops_the_microphone(voice_page, playwright_instance):
    """The same requirement, reached through the Settings privacy toggle
    rather than a command — §1 names both surfaces."""
    session = voice_page()
    assert session.live() == 1

    # setPrivacyMode() is what the Settings toggle's own listener calls;
    # invoking it directly exercises that path without needing the Voice
    # page's markup to contain another page's control.
    session.page.evaluate("setPrivacyMode(true)")

    wait_for(session, "clapState() === 'privacy-blocked'", timeout=15.0)
    assert session.settled() == (0, 0, 0)
    assert server_clap_status()["privacy_blocking"] is True


def test_the_listener_does_not_come_back_while_privacy_stays_on(voice_page):
    """A suspension resumes after a delay; privacy must not. Waiting well
    past the resume delay is the only way to tell the two apart."""
    session = voice_page()
    run_command("privacy mode on")
    wait_for(session, "clapState() === 'privacy-blocked'", timeout=15.0)
    opened_before = len(session.gum())

    session.page.wait_for_timeout(RESUME_DELAY_MS * 6)

    assert session.live() == 0
    assert len(session.gum()) == opened_before, "the microphone was reopened while privacy mode was on"
    assert session.state() == "privacy-blocked"


def test_a_clap_during_privacy_mode_cannot_reach_anything(voice_page, armed):
    """Two halves of the same guarantee: there is no worklet left to
    produce a pair, and the server refuses an activation even if one
    somehow arrived."""
    session = voice_page()
    run_command("privacy mode on")
    wait_for(session, "clapState() === 'privacy-blocked'", timeout=15.0)

    assert session.nodes() == 0, "a worklet node was still connected and could still post a pair"

    import httpx

    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        client.get("/health")
        token = client.cookies.get("jarvis_session") or ""
        response = client.post(
            "/voice/clap/activate", json={}, headers={SESSION_TOKEN_HEADER: token},
        )
    body = response.json()
    assert body["accepted"] is False
    assert body["reason"] == "privacy_mode"
    assert armed == [], "the window was asked to show while privacy mode was on"


def test_privacy_off_does_not_switch_the_feature_on_by_itself(voice_page):
    """§1: leaving privacy mode must not silently enable clap detection
    when the saved setting says off."""
    from app.voice import clap

    clap.set_enabled(False)
    session = voice_page(wait_for_listening=False)
    run_command("privacy mode on")
    session.page.wait_for_timeout(1500)
    run_command("privacy mode off")
    session.page.wait_for_timeout(RESUME_DELAY_MS * 6)

    assert session.gum() == [], "the microphone was opened for a feature that is switched off"
    assert session.state() == "disabled"


def test_startup_with_privacy_already_on_never_opens_the_microphone(voice_page):
    """Page restoration, app restart and navigation all arrive here: a
    fresh document reading its state from the server."""
    from app.core.privacy import privacy_mode

    privacy_mode.set(True)
    session = voice_page(wait_for_listening=False)
    session.page.wait_for_timeout(3000)

    assert session.gum() == [], "the microphone was opened during startup under privacy mode"
    assert session.state() == "privacy-blocked"
    assert session.errors == []


def test_repeated_privacy_cycles_leak_nothing(voice_page):
    """§1's hardest requirement. Three full cycles, then a census of every
    resource class the controller can hold."""
    session = voice_page()

    for _ in range(3):
        run_command("privacy mode on")
        wait_for(session, "clapState() === 'privacy-blocked'", timeout=15.0)
        assert session.settled() == (0, 0, 0)
        run_command("privacy mode off")
        wait_for(session, "clapListening() === true", timeout=20.0)

    assert session.live() == 1, "more than one microphone track is live"
    assert session.contexts() == 1, "AudioContexts accumulated across privacy cycles"
    assert session.nodes() == 1, "worklet nodes accumulated across privacy cycles"
    assert session.page.evaluate("__deviceChangeListeners") == 1, "devicechange listeners accumulated"
    assert session.timers() == 0, "a controller timer was left pending"
    # Four streams opened (the first plus one per cycle), all but the
    # current one stopped.
    assert len(session.gum()) == 4
    assert session.ended() == 3


# ---------------------------------------------------------------------------
# §2 The selected microphone
# ---------------------------------------------------------------------------

def first_audio_input(session: Session) -> str:
    devices = session.page.evaluate(
        "navigator.mediaDevices.enumerateDevices()"
        ".then(l => l.filter(d => d.kind === 'audioinput').map(d => d.deviceId))"
    )
    assert devices, "the fake capture device did not appear in enumerateDevices()"
    return devices[0]


def test_the_selected_microphone_reaches_get_user_media(voice_page):
    """The dropdown used to be diagnostic-only. This inspects the actual
    constraints object handed to getUserMedia, not a saved preference."""
    session = voice_page()
    device = first_audio_input(session)

    session.page.evaluate(f"setSharedMicrophone({json.dumps(device)})")
    wait_for(session, "clapListening() === true", timeout=20.0)

    last = session.gum()[-1]
    assert last["constraints"]["audio"]["deviceId"] == {"exact": device}, (
        "the chosen microphone never reached getUserMedia"
    )
    assert session.page.evaluate("ClapController.usingFallback()") is False
    assert session.page.evaluate("document.getElementById('clap-active-device').textContent") == (
        "Using the selected microphone."
    )
    assert server_clap_status()["device_id"] == device


def test_changing_the_microphone_stops_the_previous_stream(voice_page):
    """§2 in one line: there must never be two active clap microphone
    streams."""
    session = voice_page()
    assert session.live() == 1
    device = first_audio_input(session)

    session.page.evaluate(f"setSharedMicrophone({json.dumps(device)})")
    wait_for(session, "clapListening() === true", timeout=20.0)

    assert session.live() == 1, "two clap microphone streams were live at once"
    assert session.ended() >= 1, "the previous stream was never stopped"
    assert session.contexts() == 1
    assert session.nodes() == 1


def test_a_missing_microphone_falls_back_and_says_so(voice_page):
    """A device that is gone, renamed or denied. Falling back is right;
    claiming the missing device is active is not."""
    from app.voice import clap

    clap.set_device_id("a-microphone-that-is-not-plugged-in")

    session = voice_page()

    calls = session.gum()
    assert calls[0]["constraints"]["audio"]["deviceId"] == {
        "exact": "a-microphone-that-is-not-plugged-in"
    }
    assert calls[0]["ok"] is False, "an unplugged device somehow opened"
    assert calls[0]["error"] in ("OverconstrainedError", "NotFoundError")
    assert "deviceId" not in calls[1]["constraints"]["audio"], "the fallback still pinned a device"
    assert calls[1]["ok"] is True

    assert session.live() == 1, "the fallback did not actually open a microphone"
    assert session.state() == "listening", "a missing device left the listener in a bad state"
    assert session.page.evaluate("ClapController.usingFallback()") is True
    assert session.page.evaluate("document.getElementById('clap-active-device').textContent") == (
        "The chosen microphone was unavailable, so JARVIS is using the system default."
    )


def test_a_missing_microphone_does_not_produce_a_restart_loop(voice_page):
    """The fallback happens once. A loop would show up as getUserMedia
    calls continuing to accumulate while nothing changed."""
    from app.voice import clap

    clap.set_device_id("a-microphone-that-is-not-plugged-in")
    session = voice_page()
    settled = len(session.gum())

    session.page.wait_for_timeout(3000)

    assert len(session.gum()) == settled, "the listener kept reopening the microphone"
    assert session.live() == 1


def test_a_missing_saved_device_is_shown_as_missing_in_diagnostics(voice_page):
    """The dropdown must not read as though an absent microphone is
    selected."""
    from app.voice import clap

    clap.set_device_id("a-microphone-that-is-not-plugged-in")
    session = voice_page()
    session.page.click("#diag-refresh")
    session.page.wait_for_timeout(1500)

    assert session.page.evaluate("document.getElementById('diag-device-select').value") == ""
    assert "not connected" in session.page.evaluate(
        "document.getElementById('diag-device-missing').textContent"
    )


def test_an_unrelated_device_change_does_not_restart_the_listener(voice_page):
    """Plugging in a webcam is not a reason to reopen an audio stream."""
    session = voice_page()
    before = len(session.gum())

    session.page.evaluate("navigator.mediaDevices.dispatchEvent(new Event('devicechange'))")
    session.page.wait_for_timeout(1500)

    assert len(session.gum()) == before, "an unrelated device change restarted the microphone"
    assert session.live() == 1
    assert session.state() == "listening"


def test_losing_the_active_microphone_restarts_cleanly(voice_page):
    """The software half of unplugging the microphone in use: it stops
    being listed, and a devicechange arrives."""
    session = voice_page()
    before = len(session.gum())

    session.page.evaluate("__hideDevice(ClapController.activeDeviceId())")
    session.page.evaluate("navigator.mediaDevices.dispatchEvent(new Event('devicechange'))")
    session.page.wait_for_function(
        f"__clapGumCalls().length > {before}", timeout=15000,
    )
    wait_for(session, "clapListening() === true", timeout=20.0)

    assert session.live() == 1, "losing a device left more or less than one live stream"
    assert session.ended() >= 1, "the lost device's track was never stopped"
    assert session.contexts() == 1
    assert session.nodes() == 1
    assert session.errors == []


# ---------------------------------------------------------------------------
# §5 Suspension
# ---------------------------------------------------------------------------

def test_overlapping_suspension_reasons_are_reference_counted(voice_page):
    """§5: "Ending one reason must not resume listening while another
    reason remains active." Two in, one out, still silent."""
    session = voice_page()

    session.page.evaluate("clapSuspend('speaking'); clapSuspend('push-to-talk');")
    wait_for(session, "clapState() === 'suspended'", timeout=10.0)
    assert session.settled() == (0, 0, 0), "suspension left the microphone open"

    session.page.evaluate("clapResume('speaking')")
    session.page.wait_for_timeout(RESUME_DELAY_MS * 5)
    assert session.live() == 0, "the listener resumed while push-to-talk still held it"
    assert session.state() == "suspended"

    session.page.evaluate("clapResume('push-to-talk')")
    wait_for(session, "clapListening() === true", timeout=15.0)
    assert session.live() == 1


def test_releasing_the_same_reason_twice_does_not_resume_early(voice_page):
    session = voice_page()
    session.page.evaluate("clapSuspend('speaking'); clapSuspend('push-to-talk');")
    wait_for(session, "clapState() === 'suspended'", timeout=10.0)

    session.page.evaluate("clapResume('speaking'); clapResume('speaking');")
    session.page.wait_for_timeout(RESUME_DELAY_MS * 5)

    assert session.live() == 0, "a double release resumed the listener early"
    assert session.state() == "suspended"


def test_an_exception_inside_a_suspended_operation_still_releases_it(voice_page):
    """withClapSuspended() releases in a `finally`. A speech engine that
    throws must not leave the listener suspended for good."""
    session = voice_page()

    session.page.evaluate(
        "withClapSuspended('speaking', async () => { throw new Error('engine failed'); })"
        ".catch(() => {})"
    )
    wait_for(session, "clapListening() === true", timeout=15.0)

    assert session.live() == 1
    assert session.page.evaluate("ClapController.suspendedBy()") == []


def test_push_to_talk_suspends_and_releases_the_clap_listener(voice_page):
    """setPttState is the single choke point every push-to-talk path goes
    through, so driving it drives all of them."""
    session = voice_page()

    session.page.evaluate("setPttState(PTT_STATE.LISTENING)")
    wait_for(session, "clapState() === 'suspended'", timeout=10.0)
    assert session.settled() == (0, 0, 0), "the clap listener held the microphone during push-to-talk"

    session.page.evaluate("setPttState(PTT_STATE.IDLE)")
    wait_for(session, "clapListening() === true", timeout=15.0)
    assert session.live() == 1


def test_a_failed_microphone_test_still_releases_the_clap_listener(voice_page):
    """The diagnostics level meter takes the microphone for a few seconds.

    When it cannot open one at all, it still has to give the clap
    listener back. Before this test the denied-permission path returned
    early without releasing its reason, and the listener stayed suspended
    until the page was closed — a five-second level test that failed was
    no reason to stop listening for claps for good.
    """
    session = voice_page()
    session.page.evaluate("__denyNonClapMicrophone()")

    session.page.click("#diag-test-mic")
    session.page.wait_for_function(
        "document.getElementById('diag-test-message').textContent.indexOf('denied') !== -1",
        timeout=15000,
    )

    assert session.page.evaluate("ClapController.suspendedBy()") == [], (
        "the microphone test kept its suspension after failing"
    )
    wait_for(session, "clapListening() === true", timeout=15.0)
    assert session.live() == 1


def test_a_failed_transcription_still_releases_the_listener(voice_page):
    """The error path out of push-to-talk. A reason held forever is the
    same defect as a reason released too early, in the other direction."""
    session = voice_page()

    session.page.evaluate("setPttState(PTT_STATE.RECORDING)")
    wait_for(session, "clapState() === 'suspended'", timeout=10.0)
    session.page.evaluate("setPttState(PTT_STATE.ERROR, 'transcription failed')")

    wait_for(session, "clapListening() === true", timeout=15.0)
    assert session.page.evaluate("ClapController.suspendedBy()") == []


# ---------------------------------------------------------------------------
# §3 Calibration
# ---------------------------------------------------------------------------

def test_calibration_measures_a_real_pair_and_proposes_settings(voice_page, tmp_path):
    """A real double clap, through the real capture path, into the real
    worklet. The onsets are synthesised from code — deterministic
    amplitude transients, never a recording of anybody's room."""
    session = voice_page(wav=late_pair_clip(tmp_path))

    session.page.click("#clap-cal-start")
    wait_for(session, "clapState() === 'calibrating'", timeout=15.0)
    assert session.live() == 1, "calibration did not open exactly one microphone"

    session.page.wait_for_function(
        "document.getElementById('clap-cal-proposal').textContent.indexOf('Proposed:') === 0",
        timeout=20000,
    )
    message = session.page.evaluate("document.getElementById('clap-cal-message').textContent")
    assert "First clap detected." in message
    assert "Second clap detected" in message
    assert "Double clap accepted." in message

    # It let go of the microphone on its own, and the ordinary listener
    # came back rather than a second stream being opened alongside it.
    wait_for(session, "clapListening() === true", timeout=20.0)
    assert session.live() == 1
    assert session.contexts() == 1
    assert session.nodes() == 1


def test_calibration_never_sends_a_measurement_anywhere(voice_page, tmp_path):
    """Every request the page made, inspected. Not one carries an onset.

    The static test in tests/test_clap.py proves no code posts them; this
    proves no request contained them while a calibration was actually
    running.
    """
    session = voice_page(wav=late_pair_clip(tmp_path))
    session.page.click("#clap-cal-start")
    session.page.wait_for_function(
        "document.getElementById('clap-cal-proposal').textContent.indexOf('Proposed:') === 0",
        timeout=25000,
    )

    forbidden = {"at", "peak", "onset", "onsets", "rms", "level", "sample", "audio", "waveform"}
    for request in session.requests():
        if not request["body"]:
            continue
        try:
            decoded = json.loads(request["body"])
        except ValueError:
            continue
        leaked = keys_at_any_depth(decoded) & forbidden
        assert not leaked, f"{request['url']} carried {sorted(leaked)}"


def test_saving_a_calibration_stores_only_clamped_tuning(voice_page, tmp_path):
    """Nothing is written without an explicit Save, and what is written
    is inside app/voice/clap.py's SAFE_BOUNDS."""
    from app.voice import clap

    session = voice_page(wav=late_pair_clip(tmp_path))
    session.page.click("#clap-cal-start")
    session.page.wait_for_function(
        "document.getElementById('clap-cal-proposal').textContent.indexOf('Proposed:') === 0",
        timeout=25000,
    )
    assert clap.tuning() == {}, "something was saved before Save was pressed"

    session.page.click("#clap-cal-save")
    session.page.wait_for_function(
        "document.getElementById('clap-cal-message').textContent.indexOf('Saved.') === 0",
        timeout=15000,
    )

    saved = clap.tuning()
    assert saved, "Save stored nothing"
    for key, value in saved.items():
        low, high = clap.SAFE_BOUNDS[key]
        assert low <= value <= high, f"{key}={value} is outside {low}..{high}"
    assert saved["maxGap"] - saved["minGap"] >= clap.MIN_GAP_SPREAD


def test_resetting_a_calibration_returns_to_the_standard_settings(voice_page):
    from app.voice import clap

    clap.set_tuning({"absMin": 0.02, "minGap": 0.1, "maxGap": 0.6})
    session = voice_page()

    session.page.click("#clap-cal-reset")
    session.page.wait_for_function(
        "document.getElementById('clap-cal-message').textContent.indexOf('Reset') === 0",
        timeout=15000,
    )

    assert clap.tuning() == {}
    wait_for(session, "clapListening() === true", timeout=20.0)
    assert session.live() == 1


def test_cancelling_calibration_releases_the_microphone(voice_page):
    """Cancelling ends the calibration's *own* stream. Counting streams
    would prove nothing here — the ordinary listener legitimately starts
    a new one straight afterwards — so the assertion names the stream
    calibration was holding and requires every track on it to be ended.
    """
    session = voice_page()
    session.page.click("#clap-cal-start")
    wait_for(session, "clapState() === 'calibrating'", timeout=15.0)
    calibration_stream = len(session.streams()) - 1
    assert "live" in session.streams()[calibration_stream]

    session.page.click("#clap-cal-cancel")
    session.page.wait_for_function(
        "document.getElementById('clap-cal-message').textContent"
        ".indexOf('Calibration cancelled') === 0",
        timeout=15000,
    )

    assert all(s == "ended" for s in session.streams()[calibration_stream]), (
        "the calibration stream was still open after cancelling"
    )

    wait_for(session, "clapListening() === true", timeout=20.0)
    assert session.live() == 1, "cancelling calibration left more than one stream"
    assert session.contexts() == 1
    assert session.nodes() == 1


def test_privacy_mode_during_calibration_stops_it_immediately(voice_page):
    session = voice_page()
    session.page.click("#clap-cal-start")
    wait_for(session, "clapState() === 'calibrating'", timeout=15.0)

    run_command("privacy mode on")

    wait_for(session, "clapState() === 'privacy-blocked'", timeout=15.0)
    assert session.settled() == (0, 0, 0), "calibration kept the microphone through privacy mode"


def test_calibration_is_bounded_and_stops_itself(voice_page):
    """Nothing here may run indefinitely. Silence in, and the session ends
    on its own at ClapController.CALIBRATION_MAX_MS with the microphone
    released."""
    session = voice_page()
    bound_ms = session.page.evaluate("ClapController.CALIBRATION_MAX_MS")
    assert bound_ms <= 30000, "the calibration bound is too long to be called bounded"

    session.page.click("#clap-cal-start")
    wait_for(session, "clapState() === 'calibrating'", timeout=15.0)

    started = time.monotonic()
    session.page.wait_for_function(
        "document.getElementById('clap-cal-message').textContent"
        ".indexOf('Calibration stopped') !== -1"
        " || document.getElementById('clap-cal-message').textContent"
        ".indexOf('microphone was released') !== -1",
        timeout=int(bound_ms) + 15000,
    )
    elapsed = time.monotonic() - started
    assert elapsed <= (bound_ms / 1000.0) + 8.0, "calibration overran its own bound"

    wait_for(session, "clapListening() === true", timeout=20.0)
    assert session.live() == 1
    assert session.timers() == 0, "the calibration timer was left pending"


# ---------------------------------------------------------------------------
# §4 What the tray is told
# ---------------------------------------------------------------------------

def test_the_tray_only_says_on_when_a_microphone_is_really_open(voice_page):
    session = voice_page()
    wait_for(session, "clapState() === 'listening'", timeout=15.0)
    wait_for_server_state(session, "listening")

    assert session.live() == 1
    assert server_clap_status()["tray_label"] == "Double-clap listening: On"


def test_the_tray_says_paused_by_privacy_mode(voice_page):
    session = voice_page()
    run_command("privacy mode on")
    wait_for(session, "clapState() === 'privacy-blocked'", timeout=15.0)
    wait_for_server_state(session, "privacy-blocked")

    assert session.live() == 0
    assert server_clap_status()["tray_label"] == "Double-clap listening: Paused by Privacy Mode"


def test_the_tray_says_temporarily_paused_during_a_suspension(voice_page):
    session = voice_page()
    session.page.evaluate("clapSuspend('speaking')")
    wait_for(session, "clapState() === 'suspended'", timeout=10.0)
    wait_for_server_state(session, "suspended")

    assert server_clap_status()["tray_label"] == "Double-clap listening: Temporarily paused"


def test_the_tray_stops_saying_on_when_the_feature_is_switched_off(voice_page):
    session = voice_page()
    wait_for_server_state(session, "listening")

    session.page.click("#clap-toggle")
    session.page.wait_for_function("clapListening() === false", timeout=15000)
    wait_for_server_state(session, "disabled")

    assert server_clap_status()["tray_label"] == "Double-clap listening: Off"


def test_the_page_keeps_proving_the_microphone_is_open(voice_page):
    """app/voice/clap.py stops believing a report older than
    LISTENER_FRESH_SECONDS, so a page sitting in one state has to keep
    saying so. Without a heartbeat the tray decays to "Microphone
    unavailable" while the microphone is plainly open — dishonest in the
    other direction, and just as wrong."""
    from app.voice import clap

    session = voice_page()
    wait_for_server_state(session, "listening")
    first_report = clap._state.listener_reported_at

    session.page.wait_for_timeout(int(clap.LISTENER_FRESH_SECONDS * 1000) + 2000)

    assert clap._state.listener_reported_at > first_report, (
        "the page stopped reporting, so the tray would have gone stale while listening"
    )
    assert session.live() == 1
    assert server_clap_status()["tray_label"] == "Double-clap listening: On"


# ---------------------------------------------------------------------------
# §6 Navigation, reload and quit
# ---------------------------------------------------------------------------

def test_leaving_the_page_releases_the_microphone(voice_page):
    """The pagehide handler, exercised as a page really fires it. Asserted
    on the page's own objects while they still exist — after a real
    navigation there is nothing left to ask."""
    session = voice_page()
    assert session.live() == 1

    session.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")

    assert session.settled() == (0, 0, 0), "navigating away orphaned the microphone"
    assert session.state() == "disabled"


def test_a_quitting_page_does_not_reopen_the_microphone(voice_page):
    session = voice_page()
    session.page.evaluate("ClapController.setQuitting()")
    opened = len(session.gum())

    session.page.evaluate("refreshClap(true)")
    session.page.wait_for_timeout(RESUME_DELAY_MS * 5)

    assert session.live() == 0
    assert len(session.gum()) == opened, "a quitting page reopened the microphone"


def test_navigating_away_and_back_leaves_exactly_one_listener(voice_page):
    """A second document must not add a second listener. The counters
    reset with the document, so what this proves is that the page that
    comes back opens one stream and holds one context."""
    session = voice_page()
    session.page.goto(f"{BASE_URL}/ui/settings", wait_until="networkidle")
    session.page.goto(f"{BASE_URL}/ui/voice", wait_until="networkidle")
    session.page.wait_for_function("clapListening() === true", timeout=20000)
    session.page.wait_for_timeout(1500)

    assert session.live() == 1, "returning to the Voice page produced more than one listener"
    assert session.contexts() == 1
    assert session.nodes() == 1
    assert session.page.evaluate("__deviceChangeListeners") == 1
    assert session.errors == []


def test_a_reload_leaves_one_listener_and_an_honest_report(voice_page):
    """A renderer that reloads must come back with one listener, not two,
    and must re-establish its own report rather than inheriting the old
    document's."""
    session = voice_page()
    wait_for_server_state(session, "listening")

    session.page.reload(wait_until="networkidle")
    session.page.wait_for_function("clapListening() === true", timeout=20000)
    session.page.wait_for_timeout(1500)
    wait_for_server_state(session, "listening")

    assert session.live() == 1, "the reloaded page produced more than one listener"
    assert session.contexts() == 1
    assert session.nodes() == 1
    assert session.page.evaluate("__deviceChangeListeners") == 1
    assert server_clap_status()["tray_label"] == "Double-clap listening: On"


def test_switching_the_feature_off_releases_every_resource(voice_page):
    session = voice_page()
    assert session.live() == 1

    session.page.click("#clap-toggle")
    session.page.wait_for_function("clapListening() === false", timeout=15000)

    assert session.settled() == (0, 0, 0)
    assert session.state() == "disabled"
    assert session.timers() == 0
