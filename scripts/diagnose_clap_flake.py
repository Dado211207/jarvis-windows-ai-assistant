"""Reproduce the clap-calibration flake, with the whole timeline printed.

`tests/test_clap_controller.py`'s two calibration tests fail roughly one
run in four, in isolation and in the full browser suite alike. A summary
line saying which test failed is not enough to tell a product defect from
a test defect, so this runs the same path repeatedly and prints every
quantity that could decide it:

  * when the page loaded, the listener came up, Calibrate was pressed,
    each microphone was opened and each onset arrived;
  * the identity of the audio fixture, so "a different clip" is ruled out;
  * the detector's own numbers — `at`, `peak`, `gap` — against the
    thresholds they are compared with;
  * the controller's state at every transition;
  * which stream index the calibration actually holds, and the readyState
    of every track on every stream the controller opened;
  * what survived teardown.

**All times are `performance.now()` milliseconds** — a monotonic clock
that starts at navigation — except the detector's `at`, which is the
calibration `AudioContext`'s own `currentTime` in seconds. That pairing
is deliberate: comparing the two is what answers whether Chromium's fake
capture device restarts the WAV for each `getUserMedia` or plays one
shared timeline, and that question is the difference between "the claps
arrive 8 s after Calibrate" and "the claps may already be gone".

Run it:

    python scripts/diagnose_clap_flake.py --iterations 12
    python scripts/diagnose_clap_flake.py --iterations 12 --scenario cancel

Nothing here reads a real microphone, and nothing is written outside the
temporary directory it makes. Paths are printed as basenames: a full path
carries the account name, and this output is meant to be pasteable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

PORT = 5561
BASE_URL = f"http://127.0.0.1:{PORT}"

# Kept identical to tests/test_clap_controller.py. If these drift, the
# script stops reproducing the thing it is meant to reproduce.
QUIET_SECONDS = 30.0
FIRST_CLAP_AT = 8.00
SECOND_CLAP_AT = 8.26


# ---------------------------------------------------------------------------
# Instrumentation: the same resource tracking the suite uses, plus a clock
# ---------------------------------------------------------------------------

SLOW_MIC = """
(() => {
  // Beneath the timeline instrumentation, so the recorded resolution is
  // the delayed one - the same layering tests/test_clap_controller.py uses.
  const md = navigator.mediaDevices;
  const real = md.getUserMedia.bind(md);
  md.getUserMedia = function () {
    const mine = /clap/i.test(String(new Error().stack));
    const p = real.apply(md, arguments);
    if (!mine || !window.__slowMicMs) return p;
    return p.then(function (s) {
      return new Promise(function (r) { setTimeout(function () { r(s); }, window.__slowMicMs); });
    });
  };
})();
"""

TIMELINE = r"""
(() => {
  "use strict";
  window.__t0 = performance.now();
  window.__events = [];
  window.__mic = { calls: [], streams: [] };
  window.__ctxs = [];
  window.__nodes = [];

  function mark(kind, detail) {
    window.__events.push({
      ms: Math.round(performance.now() - window.__t0),
      kind: kind,
      detail: detail === undefined ? null : detail,
    });
  }
  window.__mark = mark;

  function fromController() {
    try { return /clap/i.test(String(new Error().stack)); } catch (e) { return false; }
  }

  const md = navigator.mediaDevices;
  const realGum = md.getUserMedia.bind(md);
  md.getUserMedia = function (constraints) {
    const mine = fromController();
    const index = window.__mic.calls.length;
    window.__mic.calls.push({ fromController: mine, ok: null });
    mark("gum-requested", { index: index, fromController: mine });
    return realGum(constraints).then(function (s) {
      window.__mic.calls[index].ok = true;
      const streamIndex = window.__mic.streams.length;
      window.__mic.streams.push({ stream: s, fromController: mine });
      mark("gum-opened", { call: index, streamIndex: streamIndex, fromController: mine });
      return s;
    }, function (e) {
      window.__mic.calls[index].ok = false;
      mark("gum-failed", { call: index, error: (e && e.name) || String(e) });
      throw e;
    });
  };

  window.AudioContext = new Proxy(window.AudioContext, {
    construct(target, args) {
      const c = Reflect.construct(target, args);
      const mine = fromController();
      window.__ctxs.push({ ctx: c, fromController: mine });
      mark("audiocontext-created", { index: window.__ctxs.length - 1, fromController: mine });
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
        mark("node-disconnected", { index: window.__nodes.indexOf(n) });
        return realDisconnect.apply(n, arguments);
      };
      window.__nodes.push(n);
      const nodeIndex = window.__nodes.length - 1;
      mark("worklet-node-created", { index: nodeIndex });

      // Observe messages with addEventListener, NOT by replacing
      // `port.onmessage`.
      //
      // `MessagePort.onmessage` is an event-handler IDL attribute:
      // assigning to it also performs an implicit `port.start()`. Defining
      // a plain JS accessor over it removes that, so the port never
      // starts, no message is ever delivered, and calibration times out —
      // an earlier version of this script did exactly that and turned a
      // 10/10 pass into a 5/5 "failure" that was entirely its own doing.
      // A diagnostic that changes the behaviour it measures is worse than
      // no diagnostic. addEventListener adds a second listener and starts
      // nothing; the controller's own assignment still starts the port.
      n.port.addEventListener("message", function (event) {
        const d = event && event.data;
        if (d && d.type === "clap-onset") {
          window.__onsets.push({
            ms: Math.round(performance.now() - window.__t0),
            node: nodeIndex, at: d.at, peak: d.peak, gap: d.gap,
          });
          mark("onset", { node: nodeIndex, at: d.at, peak: d.peak, gap: d.gap });
        } else if (d && d.type === "clap-pair") {
          window.__pairs += 1;
          mark("pair", { node: nodeIndex });
        }
      });
      return n;
    },
  });

  window.__onsets = [];
  window.__pairs = 0;

  // ── The decisive measurement ─────────────────────────────────────────
  //
  // Both failing tests wait for clapState() === "calibrating" and then
  // assert on microphone resources. Whether that is sound depends on one
  // thing: are the resources there at the instant the state flips? A
  // Python round-trip cannot answer it — the answer changes during the
  // round-trip, which is the whole point. So the page records it itself,
  // the first time it sees the state, with nothing in between.
  window.__atCalibrating = null;
  window.__watchState = function () {
    if (window.__stateWatched) return true;
    if (typeof ClapController === "undefined") return false;
    window.__stateWatched = true;
    ClapController.onChange(function (snap) {
      mark("state", { state: snap.state, listening: snap.listening });
      if (snap.state === "calibrating" && window.__atCalibrating === null) {
        window.__atCalibrating = {
          ms: Math.round(performance.now() - window.__t0),
          liveClapTracks: window.__liveClapTracks(),
          openContexts: window.__openClapContexts(),
          connectedNodes: window.__connectedClapNodes(),
          controllerStreams: window.__streamStates()
            .filter(function (x) { return x.fromController; })
            .map(function (x) { return x.tracks; }),
          listening: snap.listening,
        };
        mark("SAMPLED-AT-CALIBRATING", window.__atCalibrating);
      }
    });
    return true;
  };

  window.__streamStates = function () {
    return window.__mic.streams.map(function (s) {
      return {
        fromController: s.fromController,
        tracks: s.stream.getTracks().map(function (t) { return t.readyState; }),
      };
    });
  };
  window.__liveClapTracks = function () {
    let n = 0;
    window.__mic.streams.forEach(function (s) {
      if (!s.fromController) return;
      s.stream.getTracks().forEach(function (t) { if (t.readyState === "live") n += 1; });
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
})();
"""


# ---------------------------------------------------------------------------
# The audio, byte-identical to the suite's
# ---------------------------------------------------------------------------

def build_clips(directory: Path):
    from tests.test_clap_detection import _clap, _mix, _noise_floor, _samples, _write_wav

    quiet = _write_wav(directory / "quiet.wav", _noise_floor(_samples(QUIET_SECONDS)))

    audio = _noise_floor(_samples(QUIET_SECONDS))
    audio = _mix(audio, _clap(seed=11), FIRST_CLAP_AT)
    audio = _mix(audio, _clap(seed=12), SECOND_CLAP_AT)
    late = _write_wav(directory / "late_pair.wav", audio)
    return quiet, late


def fixture_identity(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return f"{path.name} sha256:{digest} {path.stat().st_size:,}B"


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

def start_server():
    import httpx
    import uvicorn

    from app.config import settings
    settings.jarvis_port = PORT

    from app.api.server import app as jarvis_app

    config = uvicorn.Config(jarvis_app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="clap-diagnostic-server")
    thread.start()
    for _ in range(75):
        try:
            if httpx.get(f"{BASE_URL}/health", timeout=1.0).status_code == 200:
                return server, thread
        except Exception:
            pass
        time.sleep(0.2)
    raise SystemExit("the diagnostic server did not become ready")


# ---------------------------------------------------------------------------
# One iteration
# ---------------------------------------------------------------------------

def run_once(playwright, clip: Path, scenario: str, chromium_path, slow_mic_ms: int = 0):
    """Drive the real page once. Returns a dict of everything measured."""
    result = {"scenario": scenario, "ok": False, "failure": "", "events": [], "onsets": []}

    browser = playwright.chromium.launch(
        executable_path=chromium_path,
        args=[
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
            f"--use-file-for-fake-audio-capture={clip}%noloop",
            "--autoplay-policy=no-user-gesture-required",
        ],
    )
    try:
        context = browser.new_context(permissions=["microphone"])
        page = context.new_page()
        page.add_init_script("window.__slowMicMs = %d;" % int(slow_mic_ms))
        page.add_init_script(SLOW_MIC)
        page.add_init_script(TIMELINE)
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        page.goto(f"{BASE_URL}/ui/voice", wait_until="networkidle")
        page.evaluate("__mark('page-loaded')")

        page.wait_for_function("clapListening() === true", timeout=20000)
        page.evaluate("__watchState()")
        result["listener_up_ms"] = page.evaluate(
            "(() => { __mark('listener-listening'); "
            "return Math.round(performance.now() - window.__t0); })()"
        )
        result["streams_before_click"] = page.evaluate("__streamStates()")
        result["state_before_click"] = page.evaluate("clapState()")

        # Watch the calibration worklet's messages. The node does not exist
        # yet, so the watcher is armed for the next index.
        page.evaluate("__mark('calibrate-clicked')")
        result["click_ms"] = page.evaluate("Math.round(performance.now() - window.__t0)")
        page.click("#clap-cal-start")

        if scenario == "calibrate":
            _drive_calibration(page, result)
        else:
            _drive_cancel(page, result)

        result["events"] = page.evaluate("__events")
        result["onsets"] = page.evaluate("__onsets")
        result["at_calibrating"] = page.evaluate("__atCalibrating")
        result["pairs"] = page.evaluate("__pairs")
        result["final_state"] = page.evaluate("clapState()")
        result["final_streams"] = page.evaluate("__streamStates()")
        result["live_tracks"] = page.evaluate("__liveClapTracks()")
        result["open_contexts"] = page.evaluate("__openClapContexts()")
        result["connected_nodes"] = page.evaluate("__connectedClapNodes()")
        result["page_errors"] = errors
    except Exception as exc:
        result["failure"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
        try:
            result["message"] = page.evaluate(
                "document.getElementById('clap-cal-message').textContent")
            result["events"] = page.evaluate("__events")
            result["onsets"] = page.evaluate("__onsets")
            result["final_state"] = page.evaluate("clapState()")
            result["final_streams"] = page.evaluate("__streamStates()")
            result["live_tracks"] = page.evaluate("__liveClapTracks()")
            result["open_contexts"] = page.evaluate("__openClapContexts()")
            result["connected_nodes"] = page.evaluate("__connectedClapNodes()")
        except Exception:
            pass
    finally:
        browser.close()
    return result


def _drive_calibration(page, result):
    """The path `test_calibration_measures_a_real_pair_and_proposes_settings`
    drives: wait for a proposal, then for the listener to come back."""
    page.wait_for_function(
        "document.getElementById('clap-cal-proposal').textContent.indexOf('Proposed:') === 0",
        timeout=20000,
    )
    result["proposal_ms"] = page.evaluate("Math.round(performance.now() - window.__t0)")
    result["message"] = page.evaluate(
        "document.getElementById('clap-cal-message').textContent")
    page.wait_for_function("clapListening() === true", timeout=20000)
    result["relisten_ms"] = page.evaluate("Math.round(performance.now() - window.__t0)")
    result["ok"] = True


def _drive_cancel(page, result):
    """The path `test_cancelling_calibration_releases_the_microphone`
    drives — including the exact index arithmetic the test performs, so a
    mis-indexed assertion reproduces here rather than being smoothed over."""
    page.wait_for_function("clapState() === 'calibrating'", timeout=15000)
    result["calibrating_ms"] = page.evaluate("Math.round(performance.now() - window.__t0)")

    # Precisely what the test does: assume the LAST controller stream is
    # calibration's. Recorded rather than trusted.
    controller_streams = page.evaluate(
        "__streamStates().filter(s => s.fromController)")
    result["controller_streams_at_calibrating"] = controller_streams
    picked = len(controller_streams) - 1
    result["test_picks_stream_index"] = picked
    result["picked_tracks_at_calibrating"] = (
        controller_streams[picked]["tracks"] if controller_streams else [])
    result["test_first_assert_would_pass"] = bool(
        controller_streams and "live" in controller_streams[picked]["tracks"])

    page.click("#clap-cal-cancel")
    page.wait_for_function(
        "document.getElementById('clap-cal-message').textContent"
        ".indexOf('Calibration cancelled') === 0",
        timeout=15000,
    )
    after = page.evaluate("__streamStates().filter(s => s.fromController)")
    result["controller_streams_after_cancel"] = after
    result["picked_tracks_after_cancel"] = (
        after[picked]["tracks"] if picked < len(after) else [])
    result["test_second_assert_would_pass"] = bool(
        picked < len(after) and all(s == "ended" for s in after[picked]["tracks"]))

    page.wait_for_function("clapListening() === true", timeout=20000)
    result["relisten_ms"] = page.evaluate("Math.round(performance.now() - window.__t0)")
    result["ok"] = (
        result["test_first_assert_would_pass"] and result["test_second_assert_would_pass"])
    if not result["ok"]:
        result["failure"] = (
            "the test's stream-index assumption did not hold: "
            f"picked index {picked}, "
            f"tracks at calibrating {result['picked_tracks_at_calibrating']}, "
            f"tracks after cancel {result['picked_tracks_after_cancel']}"
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_iteration(n: int, clip: Path, result: dict, thresholds: dict) -> None:
    verdict = "PASS" if result["ok"] else "FAIL"
    print(f"\n--- iteration {n:02d} · {result['scenario']} · {verdict} " + "-" * 28)
    print(f"  audio fixture        : {fixture_identity(clip)}")
    print(f"                         claps mixed in at {FIRST_CLAP_AT}s and {SECOND_CLAP_AT}s "
          f"(gap {SECOND_CLAP_AT - FIRST_CLAP_AT:.2f}s)")
    print(f"  thresholds           : absMin={thresholds['absMin']} "
          f"minGap={thresholds['minGap']} maxGap={thresholds['maxGap']} "
          f"refractory={thresholds['refractory']} CALIBRATION_MAX_MS={thresholds['maxMs']}")
    print(f"  state before click   : {result.get('state_before_click')!r}")
    sample = result.get("at_calibrating")
    if sample:
        verdict_live = "OK" if sample["liveClapTracks"] == 1 else "ZERO — the race"
        print(f"  AT the instant state became 'calibrating' ({sample['ms']} ms):")
        print(f"      live clap tracks : {sample['liveClapTracks']}  <- {verdict_live}")
        print(f"      open contexts    : {sample['openContexts']}")
        print(f"      connected nodes  : {sample['connectedNodes']}")
        print(f"      controller streams: {sample['controllerStreams']}")
        print(f"      isListening()    : {sample['listening']}")
    print(f"  listener up at       : {result.get('listener_up_ms')} ms")
    print(f"  Calibrate clicked at : {result.get('click_ms')} ms")

    if result.get("calibrating_ms") is not None:
        print(f"  state=calibrating at : {result['calibrating_ms']} ms")
    if result.get("proposal_ms") is not None:
        print(f"  proposal shown at    : {result['proposal_ms']} ms")
    if result.get("relisten_ms") is not None:
        print(f"  listening again at   : {result['relisten_ms']} ms")

    onsets = result.get("onsets") or []
    print(f"  detector onsets      : {len(onsets)}")
    for i, onset in enumerate(onsets):
        print(f"      onset {i}: at={onset['at']:.3f}s (AudioContext clock) "
              f"peak={onset['peak']:.4f} gap={onset['gap']:.3f}s "
              f"arrived {onset['ms']} ms after navigation")
    if len(onsets) >= 2:
        measured = onsets[1]["at"] - onsets[0]["at"]
        print(f"      measured gap: {measured:.3f}s "
              f"(fixture {SECOND_CLAP_AT - FIRST_CLAP_AT:.2f}s, "
              f"window {thresholds['minGap']}..{thresholds['maxGap']})")
    print(f"  pairs emitted        : {result.get('pairs')}")
    if result.get("message"):
        print(f"  calibration message  : {result['message'][:170]!r}")

    if result.get("test_picks_stream_index") is not None:
        print(f"  test picks stream    : index {result['test_picks_stream_index']} "
              f"of {len(result.get('controller_streams_at_calibrating') or [])}")
        print(f"      at 'calibrating' : {result.get('picked_tracks_at_calibrating')} "
              f"→ first assert {'passes' if result.get('test_first_assert_would_pass') else 'FAILS'}")
        print(f"      after cancel     : {result.get('picked_tracks_after_cancel')} "
              f"→ second assert {'passes' if result.get('test_second_assert_would_pass') else 'FAILS'}")

    print(f"  final UI state       : {result.get('final_state')!r}")
    print(f"  streams (controller) : {[s['tracks'] for s in (result.get('final_streams') or []) if s['fromController']]}")
    print(f"  teardown remaining   : live_tracks={result.get('live_tracks')} "
          f"open_contexts={result.get('open_contexts')} "
          f"connected_nodes={result.get('connected_nodes')}")
    print(f"  page errors          : {result.get('page_errors') or []}")
    if result["failure"]:
        print(f"  FAILURE              : {result['failure']}")
    print("  timeline             :")
    for event in result.get("events") or []:
        print(f"      {event['ms']:>6} ms  {event['kind']:<22} {json.dumps(event['detail'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=6)
    parser.add_argument("--scenario", choices=("calibrate", "cancel", "both"), default="both")
    parser.add_argument("--slow-mic-ms", type=int, default=0,
                        help="delay the controller getUserMedia resolution by this many ms")
    parser.add_argument("--quiet-timeline", action="store_true",
                        help="omit the per-event timeline (keeps the summary)")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    from tests.conftest import chromium_executable_path

    workdir = Path(tempfile.mkdtemp(prefix="clap-diagnostic-"))
    try:
        quiet, late = build_clips(workdir)
        server, thread = start_server()

        from app.core.privacy import privacy_mode
        from app.voice import clap
        import app.launcher.attention as attention
        attention.request = lambda: True          # never touch a real AppData marker
        clap.reset_for_tests()
        clap.set_enabled(True)
        privacy_mode.set(False)

        detector = clap.detector_settings()
        thresholds = {
            "absMin": detector.get("absMin"),
            "minGap": detector.get("minGap"),
            "maxGap": detector.get("maxGap"),
            "refractory": detector.get("refractory"),
            "maxMs": 15000,
        }

        scenarios = (["calibrate", "cancel"] if args.scenario == "both" else [args.scenario])
        tally = {s: {"pass": 0, "fail": 0} for s in scenarios}

        with sync_playwright() as playwright:
            for n in range(1, args.iterations + 1):
                for scenario in scenarios:
                    clip = late if scenario == "calibrate" else quiet
                    result = run_once(
                        playwright, clip, scenario, chromium_executable_path(),
                        slow_mic_ms=args.slow_mic_ms)
                    if args.quiet_timeline:
                        result = dict(result, events=[])
                    print_iteration(n, clip, result, thresholds)
                    tally[scenario]["pass" if result["ok"] else "fail"] += 1

        print("\n" + "=" * 72)
        failed = 0
        for scenario, counts in tally.items():
            total = counts["pass"] + counts["fail"]
            print(f"{scenario:<12} {counts['pass']}/{total} passed, "
                  f"{counts['fail']} failed")
            failed += counts["fail"]
        print("=" * 72)

        server.should_exit = True
        thread.join(timeout=5.0)
        return 1 if failed else 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
