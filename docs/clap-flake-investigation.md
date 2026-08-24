# The clap calibration flake: what it actually was

Two tests in `tests/test_clap_controller.py` failed roughly one run in
four, in isolation and inside the full browser suite alike:

* `test_calibration_measures_a_real_pair_and_proposes_settings`
* `test_cancelling_calibration_releases_the_microphone`

Observed rate before this pass: **2 failures in 9 isolated runs of the
file, 2 failures in 8 full-browser-suite runs**. A passing retry was
never accepted as a resolution, and neither was "it passes in isolation"
— that claim was made earlier in PR #15's description on too few samples
and is withdrawn.

Reproduce any of this with:

```
python scripts/diagnose_clap_flake.py --iterations 10
```

---

## The measurement that settled it

Both tests wait for `clapState() === 'calibrating'` and then immediately
assert on microphone resources. Whether that is sound depends on exactly
one question: **are those resources open at the instant the state flips?**

A Python round trip cannot answer it, because the answer changes during
the round trip — which is the whole problem. So the page records it
itself, inside `ClapController.onChange`, with nothing in between:

```
AT the instant state became 'calibrating' (1306 ms):
    live clap tracks : 0
    open contexts    : 0
    connected nodes  : 0
    controller streams: [['ended']]
    isListening()    : False
```

**12 of 12 iterations, without exception.** The only stream in existence
at that moment is the ordinary listener's, and `startCalibration()` has
already stopped it.

The microphone opens shortly afterwards:

| iteration | `state = calibrating` | calibration `getUserMedia` resolved | window |
|---|---|---|---|
| calibrate 01 | 1655 ms | 1668 ms | 13 ms |
| cancel 01 | 1312 ms | 1324 ms | 12 ms |
| calibrate 02 | 1491 ms | 1498 ms | 7 ms |
| cancel 02 | 1291 ms | 1298 ms | 7 ms |
| calibrate 03 | 1617 ms | 1628 ms | 11 ms |
| cancel 03 | 1450 ms | 1472 ms | 22 ms |
| calibrate 04 | 1453 ms | 1465 ms | 12 ms |
| cancel 04 | 1289 ms | 1297 ms | 8 ms |
| calibrate 05 | 1364 ms | 1377 ms | 13 ms |
| cancel 05 | 1316 ms | 1324 ms | 8 ms |
| calibrate 06 | 1339 ms | 1349 ms | 10 ms |
| cancel 06 | 1396 ms | 1407 ms | 11 ms |

**n = 12, min 7 ms, median 11 ms, max 22 ms.**

`page.wait_for_function` polls on `requestAnimationFrame` — about every
16 ms on an idle machine, and less often under load. The window is
therefore one to two poll ticks wide, and under load it widens at both
ends: `getUserMedia` takes longer *and* the poll that would have skipped
past it arrives later.

## Root cause

**An assertion sampling an intermediate state.** The controller
publishes `CALIBRATING` when calibration is *requested*; the two tests
read it as "calibration is capturing".

`app/ui/static/clap-controller.js::startCalibration()` in order:

```js
stopCalibration();
teardown();                       // the ordinary listener's mic is now closed
calibration = { … };
setState(STATE.CALIBRATING);      // <- the state the tests wait for
const ok = await start(opts, …);  // <- the microphone opens HERE, 7-22 ms later
```

What each test then does inside that window:

| Test | Line | Assertion | Expected | Actual inside the window |
|---|---|---|---|---|
| `…measures_a_real_pair…` | `assert session.live() == 1` | "calibration did not open exactly one microphone" | `1` | **`0`** |
| `…cancelling_calibration…` | `calibration_stream = len(session.streams()) - 1`; `assert "live" in session.streams()[calibration_stream]` | the calibration stream, live | `['live']` | **`['ended']`** — index 0 is the *torn-down listener*, because calibration's stream does not exist yet |

Both tests pass whenever the round trip outlasts the window, which is
most of the time. That is the entire flake.

## What was ruled out, and with what evidence

| Candidate | Verdict | Evidence |
|---|---|---|
| Audio-fixture timing | **Not it** | Chromium restarts the WAV for each `getUserMedia`: the first onset lands at `at = 8.011–8.060 s` on the calibration `AudioContext`'s own clock against a clap mixed in at 8.00 s. The claps are anchored to calibration start, so the margin to `CALIBRATION_MAX_MS` is ~6.9 s **regardless of how slowly the page loaded**. |
| Detector / threshold / floating point | **Not it** | Peaks `1.2250` and `1.2372` and gap `0.258–0.261 s` reproduce identically every run, against `absMin 0.035` and a `0.12–0.7 s` window. Nothing is near a boundary. |
| Debounce / cooldown surviving a run | **Not it** | `refractory` is 1.5 s and is never approached; each calibration builds a **new** `AudioWorkletNode`, so the detector's state starts fresh by construction. |
| Stale state between tests | **Not it** | Every test gets its own browser process, context and page. |
| Wall clock where monotonic is required | **Not it** | The detector uses `currentTime` (the `AudioContext`'s monotonic clock) and the bound uses `setTimeout`. No `Date.now()` anywhere on the path. |
| A surviving task, thread or process | **Not it** | The `live_tracks=1 open_contexts=1 connected_nodes=1` at the end of a run is the ordinary listener correctly resumed — visible in the timeline as a third `getUserMedia` after cancellation, and asserted as exactly one. |
| Fixed sleeps standing in for state | **Not present** | Neither test sleeps; both wait for a condition. The condition was the wrong one. |

## Two product defects found in the same code path

Neither causes the flake. Both were found by reading the path the
evidence pointed at, and both are fixed here.

**1. A superseded calibration wedges the state machine.**

```js
const ok = await start(opts, onCalibrationMessage);
if (!ok) { calibration = null; return { ok: false, reason: "superseded" }; }
```

`calibration` is nulled, the state is left at `CALIBRATING`, and
`reconcile()` is never called. `reconcile()`'s own rule is
`calibration → CALIBRATING`, so `calibration === null` with
`state === CALIBRATING` is an invariant violation: the page shows
"calibrating" with no microphone open and nothing scheduled to recover.
Reachable whenever a teardown (privacy, device change, `configure`,
quit) lands inside the same `getUserMedia`/`addModule` window the flake
exploits.

**2. The calibration bound did not cover acquiring the microphone.**

```js
const ok = await start(opts, onCalibrationMessage);   // unbounded
calibration.timer = setTimeout(…, CALIBRATION_MAX_MS); // armed only afterwards
```

If `getUserMedia` hangs — a device in use, a permission prompt, a driver
stall — calibration sits in `CALIBRATING` indefinitely with no timer
armed. CLAUDE.md requires calibration to be bounded and to release the
microphone "on success, timeout, cancel, navigation, privacy and quit";
an unbounded acquisition phase is not bounded. The bound is now armed
**before** the await, so it covers acquisition too.

## What was inspected and found correct

* `state === 'calibrating'` while the microphone is closed is **honest**:
  `notify()` publishes `listening: isListening()` alongside it, which is
  `false`, and the tray reads that field. No surface claims a live
  microphone that is not open.
* The first onset carries `gap = at − (−999) ≈ 1007 s`, which looks
  alarming and is never shown: `describeOnsets()` reads `onsets[1].gap`,
  the real inter-clap interval.
* `teardown()` is synchronous and idempotent, and the generation counter
  correctly discards a stream opened by a superseded `start()`.

## A note on the diagnostic itself

The first version of `scripts/diagnose_clap_flake.py` observed the
detector by replacing `port.onmessage` with a JS accessor. That silently
broke the run: `MessagePort.onmessage` is an event-handler IDL
attribute whose assignment performs an implicit `port.start()`, so the
port never started, no message was ever delivered, and calibration timed
out — turning a 10/10 pass into a 5/5 "failure" that was entirely the
instrument's doing. It now uses `addEventListener`, which starts
nothing. **A diagnostic that changes the behaviour it measures is worse
than no diagnostic**, and the 5/5 result it produced is reported in
PR #15 as an invalid run rather than quietly dropped.
