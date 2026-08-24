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


---

# Round two: the fix was incomplete, and CI was red the whole time

The wait fix above is correct and stays. It was **not** the whole cause.

## What the first round got wrong

A 30-iteration stress run on `a0e8f9a` — the commit whose message said
the flake was fixed — came back **6 failures in 30 (20%)**, and GitHub's
Playwright job failed the same test on the same commit. The earlier "6
consecutive runs, all clean" was six samples of a one-in-five event: an
82% chance of seeing exactly that if nothing had changed. It should not
have been reported as a result.

## The second cause: a clap whose attack straddles a block boundary

Failing runs report **one** onset with `peak=1.2372`; passing runs report
two, `1.2250` then `1.2372`. `1.2372` is the *second* clap's waveform, so
the detector was missing the **first** clap entirely and reporting the
second as "First clap detected."

Simulating `clap-processor.js` block by block over the exact fixture PCM
and sweeping the 128 possible block phases shows why, on one line:

```
block at 8.0000s: rms=0.51758  prevRms=0.01268  gate=0.01225
```

The block *immediately before* the clap caught a sliver of its attack.
That lifted its RMS from the noise floor (~0.0023) to `0.01268` — over
`threshold * attackFall` by **0.00043**. `sharp` is therefore false for
the loud block that follows; every later block has an even higher
`prevRms`; the whole clap is swallowed. A first clap lost this way is a
double clap that never happens.

The 500 ms `getUserMedia` delay in the new test did not *cause* this. It
decorrelated the stream start from the file start, which is what exposes
it — and which is the normal condition on a real machine.

| | missed onset | false pairs on speech / hum / silence / single / mistimed |
|---|---|---|
| before | 2–3 of 128 block phases | 0 |
| after (`loudBlocks <= 2`) | **0 of 128** | **0**, all 128 phases each |

The fix adds one integer — a count of consecutive above-gate blocks — and
treats "the signal only just rose" as an attack alongside "the previous
block was quiet". It is a count of blocks, not audio; no array, no
history, nothing reconstructable. Speech ramps over tens of
milliseconds and is still rejected, verified at every block phase against
the suite's own speech, hum, silence, single-clap and mistimed-pair
fixtures.

**Browser confirmation:** 12/12 at the 500 ms delay that previously
failed 2 in 10, with both claps detected every time.

## The regression test, and how its first version was worthless

`test_a_clap_is_detected_whatever_block_phase_its_attack_lands_on` renders
the **real worklet** in an `OfflineAudioContext`, which starts at sample 0
in aligned 128-sample quanta — so a clap placed at sample `48000 + phase`
lands at exactly that phase, every run, on every machine.

Its first version synthesised the audio in JavaScript with its own
pseudo-random generator. It **passed against the broken detector**,
because the defect is a block landing 0.00043 above a gate and a different
random sequence never reaches that edge. The audio is now the exact
16-bit PCM the WAV fixtures contain, transferred as base64. Reverted, the
detector now fails phases 62, 126 and 127 and passes phase 0 — the
control.

## The other thing round one missed entirely: `ci.yml` was never checked

`ci.yml` has failed on **every commit of this corrective pass** — runs
#148 through #155, `5f4cdd4` to `a0e8f9a`. The last green was #147 on
`9bb5439`, before the Coding Workspace work began. `fc67065`, reported as
verified with two green acceptance runs, was carrying
`15 failed, 2827 passed` the whole time.

Only `windows-installer.yml` was ever inspected. "Gate x2 green" was true
of *local* runs and was reported as though it meant CI.

**The failures:** all fifteen in `tests/test_coding_browser_qa.py`, all
`reason='The browser connection closed.'`, `opened=False`, with 6-9
browser processes already gone by cleanup time.

**Why:** that file drives a real browser and is deliberately not behind
the `browser` marker — on Windows it runs against the Edge every target
machine has, which is the point of the suite. But `ci.yml`'s default job
installed no browser at all, so `browser_engine` fell through PATH to
whatever `google-chrome` the runner image ships, and that did not start.

**Why nobody could tell:** `_OwnedBrowser.start()` sent the browser's
stderr to `DEVNULL`. The one artefact that would have named the cause was
discarded, leaving a sentence that says only that something ended.

Two fixes:

* the browser's stderr is captured to a temporary file, and its last few
  lines — path-stripped, bounded, noise-filtered — are reported. A
  browser that dies at startup is now `ENGINE_UNAVAILABLE` with its own
  words, not `FAILED` with a shrug;
* `ci.yml`'s default job installs the same Chromium the Playwright job
  does. `browser_engine` consults `~/.cache/ms-playwright` ahead of PATH,
  so Ubuntu and Windows exercise the same code against a browser that
  works. No test was skipped, re-marked, or had an assertion relaxed.
