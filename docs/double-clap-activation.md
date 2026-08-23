# Double-clap activation: the decision, the evidence, and the limits

This is a feature that had to justify itself before it was allowed to
exist. `CLAUDE.md`'s Safety rules ban continuous listening outright, and
the product owner granted one narrow exception for this and nothing
else. What follows is the reasoning, the measurements, and — the part
that matters most — the things it cannot do, stated plainly enough that
nobody is surprised by them on their own machine.

---

## 1. Where the listener runs, and why not in Python

**JARVIS has never had a native audio *input* dependency, and this did
not add one.**

Verified in the source, not assumed:

- Playback is `winsound` (`app/voice/audio.py`), the Windows standard
  library. Output only.
- Push-to-talk records in the browser, not in Python:
  `navigator.mediaDevices.getUserMedia` and `MediaRecorder` in
  `app/ui/static/app.js`, POSTing the finished blob to the server.
- A repository-wide search for `sounddevice`, `pyaudio`, `PyAudio` and
  `portaudio` finds no dependency and no import — only a test that
  asserts their absence (`tests/test_speak_intent.py`).

So a Python-side clap listener would have meant a **new native
dependency** (PortAudio via PyAudio or sounddevice). That was rejected
for three reasons, in order of weight:

1. **It could not be tested where it runs.** GitHub-hosted
   `windows-latest` runners have no audio input device at all —
   `actions/runner-images` issue #2528 records that even `winsound`
   fails there for want of an audio device, with the only workarounds
   being a virtual audio driver or a self-hosted runner. A listener that
   CI cannot start is a listener whose start/stop lifecycle cannot be
   asserted, and the requirement for this feature was explicitly that it
   be lifecycle-tested.
2. **It could not be tested here either.** This project's development
   container is Linux with no audio device.
3. **It would have to be bundled by PyInstaller and shipped.** A native
   extension in the installer, for a feature that is off by default, for
   a capability the product already has in the page that is already
   open.

The browser path has none of those problems, and one decisive advantage:
it can be tested for real. Chromium accepts
`--use-file-for-fake-audio-capture=<file.wav>`, which presents a WAV
file to the page as a microphone. Everything below the capture device is
then the real thing — the same `getUserMedia` constraints the product
asks for, the same `AudioWorklet` on the same audio thread, the same
`POST /voice/clap/activate` into the same server.

`tests/test_clap_detection.py` is that test, and it runs in CI's
`browser-tests` job.

**Why an `AudioWorklet` and not the `AnalyserNode` the level meter
uses.** The diagnostics level meter reads its analyser from
`requestAnimationFrame`, which Chromium throttles hard in a hidden or
backgrounded document — fine for a meter somebody is watching, useless
for a listener that must keep working while JARVIS is minimised. An
`AudioWorkletProcessor` runs on the audio rendering thread, which is
not throttled.

---

## 2. What the detector actually computes

Per 128-sample render quantum (≈2.7 ms at 48 kHz): **root-mean-square
and peak amplitude**. Two numbers. That is the complete feature set, and
it is the reason this is honestly describable as a transient counter
rather than a listener — there is nothing in two amplitude values that
could distinguish a word from a door.

The state machine:

- A slow exponential average tracks the background level, updated only
  while nothing is happening (or a clap would raise the floor it is
  measured against).
- An **onset** requires the block to cross `max(background × ratio,
  absolute floor)` *and* the preceding block to have been well below it.
  That second condition is what separates a clap from speech: a clap
  goes from background to peak inside one block, whereas speech ramps
  over tens of milliseconds, so at the instant speech crosses the
  threshold its previous block is just under it, not far under it.
- A **clap** is an onset that decays back below half-threshold within
  ~160 ms. Longer and it is a sustained sound; the pairing state is
  cleared and onsets are suppressed briefly, so a dip in the middle of a
  sentence cannot look like a fresh attack.
- A **pair** is two claps separated by 120–700 ms. Firing starts a
  1.5 s refractory period.

Six scalars of state, all overwritten every block. No ring buffer, no
history, no samples retained past the call that produced them.

---

## 3. The measurements

The thresholds in `app/voice/clap.py::SENSITIVITY_PROFILES` were derived
from this experiment, not guessed and not copied from anywhere. Six
synthesised clips, each 6 seconds, played through a real Chromium's fake
capture device at 44.1 kHz:

| Clip | What it is | Fires? |
|---|---|---|
| Two claps | 12 ms-decay broadband bursts, 260 ms apart | **yes** |
| One clap | one of them | no |
| Mistimed claps | two, 1.6 s apart — outside the pairing window | no |
| Speech | 4 Hz amplitude-modulated tone-plus-noise, *louder* than the claps | no |
| Sustained tone | 220 Hz hum well over the threshold | no |
| Near silence | the noise floor alone | no |

The speech clip is deliberately louder than the claps. A detector that
only asked "is this loud" would fire on it repeatedly; requiring a sharp
attack and a short decay is what makes the difference, and this is the
clip that proves it.

Those six cases are `tests/test_clap_detection.py`, run end-to-end
through the product: the page's own boot code starts the listener from
the server's stored setting, and the assertion is on the server's
activation count. Three more tests in the same file cover the lifecycle
— the listener does not start while the feature is off, privacy mode
stops it, and switching it off releases the microphone stream and closes
the audio context.

---

## 4. What it is allowed to do

Exactly one thing: **ask the window to come to the front.**

That goes through `app/launcher/attention.py`, which was already in the
product for a different purpose (clicking the Start-menu shortcut while
JARVIS is already running). Its entire message is the existence of a
marker file — there is nothing in it to parse, so there is nothing to
parse wrongly, and no field in which a microphone could name an action.

`POST /voice/clap/activate` takes **no request body**. This is a design
constraint rather than an omission, and `tests/test_clap.py` asserts the
endpoint's signature so a future field cannot be added by accident.

Three server-side gates, re-checked on every activation:

1. the stored preference, which starts **off**;
2. **privacy mode** — `app/core/privacy.py`'s docstring already said any
   future listener would have to check it, and this is that listener;
3. a **refractory interval**, so a burst of transients is one
   activation.

Server-side, because a page left open before the feature was switched
off somewhere else must not still be able to act — the same reasoning as
the speech gate.

The optional greeting is spoken through the ordinary speech path and
only when `tts_service.output_enabled` is already on. Somebody who has
turned speech off gets a window, silently. It is not a second speech
switch.

---

## 5. What it cannot do — the honest limits

**It listens only while the JARVIS window exists.** The detector lives
in the page. Minimising to the tray hides the window without destroying
it, so the page and its audio thread stay alive; quitting JARVIS ends
it. This is a real limitation and the Voice page says so in as many
words. It cannot start JARVIS, because when JARVIS is not running there
is nothing listening.

**It cannot be verified from this development environment that a hidden
WebView2 window keeps the audio thread running on Windows.** Chromium
exempts pages that are capturing media from being frozen, and the audio
rendering thread is not subject to the timer throttling that affects
`requestAnimationFrame` and `setTimeout` — but that is reasoning from
Chromium's documented behaviour, not a measurement of WebView2 on a real
Windows desktop, and this document does not pretend otherwise. It is on
the physical-PC checklist for exactly that reason.

**It needs Windows microphone permission.** If the permission is denied,
the listener silently fails to start; the Voice page reports that and
points at the diagnostics panel below it, which can distinguish a denied
prompt from an absent device.

**It is not a wake word and will never become one.** Clapping cannot
send a message, run a tool, or answer a question. If what you want is
"JARVIS, do X", that is push-to-talk on the Chat page, which is
explicitly user-triggered and shows you the transcript before anything
is sent.

**Two open pages means two listeners.** Both will report the same clap;
the server's refractory interval collapses them into one activation.

---

## 6. What was considered and rejected

| Option | Why not |
|---|---|
| Python listener via PyAudio/sounddevice | New native dependency; untestable on GitHub's Windows runners (no audio device) and untestable here; would ship in the installer for an off-by-default feature |
| Wake word ("Hey JARVIS") | Requires continuous speech recognition. Banned by CLAUDE.md's Safety rules, and the exception granted was specifically *not* this |
| Clap detection on the server, over an uploaded stream | Sends audio off the audio thread and across a process boundary — precisely the thing this design exists to avoid |
| A single clap | Fires on doors, keyboards, and dropped cutlery. The pair is the discriminator |
| Shipping it enabled | A feature that opens a microphone must be opted into, not out of |

---

## 7. One controller, because the booleans were the bug

The first implementation kept the stream, the context and the node in
three module-level variables in `app.js` and decided what to do with them
from wherever the decision happened to be made. That produced the defect
this pass was opened to fix: **privacy mode repainted an indicator and
left the microphone open.**

`app/ui/static/clap-controller.js` replaces that with one explicit state
machine that owns the microphone. Nine states:

| State | Meaning |
|---|---|
| `disabled` | The feature is off, or the page is quitting |
| `starting` | `getUserMedia` is in flight |
| `listening` | A live track and an active worklet |
| `suspended` | Something else owns the audio (see §9) |
| `calibrating` | A bounded calibration session owns it |
| `privacy-blocked` | Privacy mode is on |
| `microphone-unavailable` | The microphone could not be opened |
| `stopping` | Releasing |
| `error` | An unexpected failure |

Three properties make it hold up where three booleans did not:

- **One decision point.** `reconcile()` is the only function that may
  start or tear down. Everything else — a settings change, privacy, a
  suspension, a device change, calibration, quit — sets state and calls
  it.
- **Teardown is one function and it is synchronous.** `teardown()`
  disconnects the node, disconnects the source, stops **every track**,
  and closes the context. Stopping the tracks is what actually turns the
  Windows microphone indicator off; closing the context alone does not.
  `setPrivacyBlocked(true)` calls it before it does anything else, so
  there is no window in which the indicator is still lit.
- **A generation counter.** Every `start()` remembers the generation it
  began in. A `start()` that resolves *after* a teardown belongs to a
  world that no longer exists, so it stops the stream it just opened and
  returns rather than installing a live microphone nobody asked for.
  This is the race that a set of booleans cannot express.

`isListening()` is the honest answer to "is the microphone open right
now": an active worklet node **and** at least one audio track whose
`readyState` is `live`. Nothing in the product is allowed to claim
listening on anything less.

`pagehide` tells the controller the page is going away and everything is
released. `pageshow` with `persisted` tells it the opposite: a page
restored from the back/forward cache is alive again, never runs
`DOMContentLoaded` a second time, and would otherwise still be carrying
`quitting` from its own `pagehide`. Whether a browser ever caches a page
that was holding a microphone is a decision that changes between
versions; handling both halves costs nothing when it does not.

---

## 8. The microphone is chosen, not assumed

The microphone dropdown on the Voice page used to move nothing but that
page's own level meter. It is now the **one shared choice**: saved
server-side (`mic_device_id`, on `preferences.STORABLE_KEYS`), applied to
the diagnostics meter and to the clap listener, and passed to
`getUserMedia` as `deviceId: { exact: … }`.

A device id is not a credential — it is an opaque per-origin handle to a
piece of hardware — which is why it is allowed in the preferences file at
all. It is also not stable across browsers or profiles, which is why the
page always reconciles it against `enumerateDevices()` before claiming it
is selected.

What happens when the chosen microphone is not there:

1. `getUserMedia` with `{exact: id}` rejects (`OverconstrainedError` or
   `NotFoundError`).
2. The controller retries **once**, with no device pinned, and records
   that it fell back.
3. The Voice page says *"The chosen microphone was unavailable, so JARVIS
   is using the system default."* It never claims the missing device is
   in use.
4. Diagnostics shows the dropdown fallen back to the default entry, with
   *"The microphone you chose is not connected."*

`devicechange` is bound once per page, and bound *before* the first
attempt to open a microphone rather than after a successful one — a
machine with no microphone at all still has to hear about one being
plugged in, or "microphone unavailable" would be permanent until the
page was reloaded. It restarts **only** when the device actually in use
has disappeared from `enumerateDevices()`; plugging in a webcam is not a
reason to reopen an audio stream.

---

## 9. Suspension: reference-counted, not a flag

Anything that needs exclusive audio, or that would put JARVIS's own voice
into the detector, takes a named reason:

| Reason | Taken by |
|---|---|
| `speaking` | Every speech path — Kokoro, a Windows natural voice, SAPI5, ElevenLabs, the per-message **Listen** button, **speak test**, the pronunciation preview, an automatically spoken reply |
| `push-to-talk` | `setPttState()`, the single choke point every PTT path already goes through |
| `microphone-test` | The diagnostics level meter |
| `calibrating` | A calibration session |

`suspend(reason)` / `resume(reason)` are backed by a `Set`, so two
reasons in and one out leaves the listener suspended. Resuming schedules
a reconcile after a short delay (`RESUME_DELAY_MS`), which is the
post-playback refractory: a speaker's last click must not become the
first half of a pair.

There is **one** watcher for speech rather than eight hooks. Every speech
path ends up asking the same server whether it is still speaking, so
`suspendForSpeech()` takes the reason and a single poll of
`GET /voice/speaking` releases it when the audio has finished — including
when speech was stopped, cancelled, or failed. `withClapSuspended()`
releases in a `finally`, so an exception cannot leave the listener
suspended for good.

A pair that arrives in the moment between a reason being taken and the
teardown completing is discarded rather than acted on. **A clap can never
approve or execute anything**: its only route is
`POST /voice/clap/activate`, which takes no body and can only ask the
window to come forward.

---

## 10. Calibration

A bounded, explicitly started session that measures two claps and
proposes three numbers.

- It **owns the microphone for its duration**, so there is never a second
  consumer, and it releases it on success, timeout, cancel, navigating
  away, privacy mode and quit alike.
- It is bounded by construction: `CALIBRATION_MAX_MS`, a timer that is
  cleared on every exit path.
- The worklet reports per-onset scalars — a time, a peak, a gap — **only**
  when `processorOptions.calibrate` is set, which only a calibration
  session sets. `tests/test_clap.py` asserts the guard encloses the
  message.
- Those scalars are read on the page and thrown away. Nothing posts them
  anywhere; one test greps every `API.post` for them, and another
  inspects every request a real browser made during a real calibration
  session.
- Nothing is saved until **Save** is pressed. What is saved is clamped
  server-side to `SAFE_BOUNDS` — and only three values may be changed at
  all. The attack test and the sustained-sound cut-off, which are what
  separate a clap from speech, are not calibratable in either direction:
  a room that needs those relaxed is a room where this feature should
  stay off.

---

## 11. What the tray may say, and how it knows

`app/voice/clap.py::tray_label()` composes the line; the tray only
displays it. One place decides when "On" is honest.

| Line | When |
|---|---|
| `Double-clap listening: On` | The feature is on, privacy is off, and a page reported a live microphone **recently** |
| `Double-clap listening: Paused by Privacy Mode` | Privacy mode is on — checked before anything else |
| `Double-clap listening: Temporarily paused` | Suspended, starting or stopping |
| `Double-clap listening: Calibrating` | A calibration session is running |
| `Double-clap listening: Microphone unavailable` | On, but nothing has proved a microphone is open |
| `Double-clap listening: Off` | The feature is off |

The page reports its own state to `POST /voice/clap/listener` (session
token required, allowlisted values only — a status report that can carry
an arbitrary string is a channel). The report **goes stale** after
`LISTENER_FRESH_SECONDS`, so a closed or crashed tab cannot leave a false
"On" behind; and the page **re-sends it on a timer** well inside that
window, so a page that simply sits there listening does not decay into a
false "unavailable". Staleness has to cut both ways or it is not honesty,
just pessimism.

---

## 12. Demonstrating the regressions against the previous commit

`tests/test_clap_controller.py` is written so it can be run against an
*older* product commit without modification, which is how each fix here
was shown to catch the defect it was written for. No history is rewritten
and no branch is moved:

```bash
git worktree add /tmp/rc-before <previous-commit> --detach
cp tests/test_clap_controller.py tests/conftest.py /tmp/rc-before/tests/
cd /tmp/rc-before && pytest -m browser tests/test_clap_controller.py
git worktree remove /tmp/rc-before --force
```

The instrumentation the tests install is deliberately independent of the
product's internals — it wraps `getUserMedia`, the `AudioContext` and
`AudioWorkletNode` constructors, `navigator.mediaDevices`' listeners,
`setTimeout`/`clearTimeout` and `fetch` — and attributes resources by
call stack, matching both `clap-controller.js` and the older
`startClapListener`. So the headline assertion,
`__liveClapTracks() === 0` after privacy mode is enabled, means exactly
the same thing on both commits.

**Measured.** Against `4c0f67c` — the last commit before this work — all
36 tests fail:

```
36 failed in 250.75s
```

and against the commit that replaced it, all 36 pass:

```
36 passed in 176.01s
```

The headline privacy test fails on the older commit as a measurement
rather than a missing symbol:

```
Page.wait_for_function: Timeout 15000ms exceeded.
    (waiting for __liveClapTracks() === 0)
```

Fifteen seconds after privacy mode was switched on, a microphone track
opened by the clap listener was still `readyState === "live"`. That is
the defect, stated in the only terms that matter.

The other failures divide into three honest kinds, and all three are
findings rather than noise:

| Kind | Example | What it says about `4c0f67c` |
|---|---|---|
| `ReferenceError: ClapController is not defined` | most of the suspension and lifecycle tests | there was no controller — the stream, context and node were three variables and the decisions were made wherever they happened |
| `ReferenceError: setSharedMicrophone is not defined` | the selected-microphone tests | the dropdown moved nothing but the level meter |
| `Page.click: Timeout 30000ms exceeded` | every calibration test | the buttons did not exist |
| `KeyError: 'listener_state'` | the tray tests | the server had nothing to tell the tray |
