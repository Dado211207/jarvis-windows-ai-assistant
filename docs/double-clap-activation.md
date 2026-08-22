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
