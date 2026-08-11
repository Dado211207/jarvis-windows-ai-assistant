# JARVIS RC2 — what was fixed, what was proved, what was not

A report on the second repair pass, written against the ten defects
reported from real Windows 11 hardware.

The rule throughout: **a claim here is either backed by something that
ran, or it is labelled as not verified.** Several things in the "not
verified" list could have been quietly omitted. They are here instead.

---

## The ten reported defects

| # | Defect | State |
|---|---|---|
| 1 | Setup exposed Python, PowerShell, env vars, localhost URLs | **Fixed** — first run asks two questions |
| 2 | First run should ask only name + API key, key verified | **Fixed and verified** |
| 3 | App opened a browser instead of the native window | **Fixed**; WebView2 now installed by setup |
| 4 | Local AI does not work | **Partly fixed** — see the boundary below |
| 5 | Voice input does not work ("Speech runtime — Not ready") | **Fixed in the packaged app**; not verified on a real microphone |
| 6 | TTS excessively robotic | **Fixed** — Kokoro 82M neural voice |
| 7 | Tray Restart broken | **Fixed and verified** — 10 restart cycles in CI |
| 8 | Tray Quit left processes running | **Fixed and verified** — found automatically, then fixed |
| 9 | Installer should behave like a commercial Windows app | **Mostly fixed** — one gap listed |
| 10 | Full installed-product audit | This document |

---

## Defect 8 is the one worth reading about

It is the clearest evidence that the verification added in this pass is
worth having, rather than ceremony.

A ten-cycle start/quit test was added against the *real installed
application*. It failed on its first cycle, on its first run:

```
FAILED: Cycle 1: 1 WebView2 process(es) started by JARVIS
        outlived it (pids [8340]).
```

Every check that existed before it passed on that same run, because they
all asked whether `JARVIS.exe` itself had exited — which it had.

The cause was in `WindowProcess.stop()`. It asked for the window child's
descendants *after* `poll()` confirmed the child had already exited.
`process_tree.py`'s own docstring states the rule being broken: once the
parent is gone, the relationship that identifies its descendants is gone
with it, so the capture walks a dead PID and finds nothing. The kill path
got this right. The graceful path — the one taken every ordinary time
somebody chooses Quit — did not, so it cleaned up nothing, every time.

Fixed by capturing before anything is asked to stop, topping the set up
while the child is still alive (WebView2 starts helpers lazily), and
terminating it on every exit path. Three regression tests cover it, and
all three were confirmed to fail against the previous code rather than
assumed to.

---

## Defect 4: what was built, and the line that was not crossed

The defect asked JARVIS to **auto-install the Ollama runtime and
download a model**. `CLAUDE.md` forbids exactly that, in a rule marked
non-negotiable, enforced by a test that walks the AST of every module
under `app/` and restated in `docs/THREAT_MODEL.md`.

That conflict was raised rather than resolved silently. Instructed to
apply the safest choice consistent with existing instructions, the rule
stands: **JARVIS does not install Ollama and does not download models.**

Everything else in the defect was built:

* **Four states instead of one message.** "Not installed", "installed but
  not running", "running with no models" and "ready" used to produce one
  identical sentence whose suggested fix was a terminal command. Each now
  has its own explanation and its own next step.
* **Installed-but-stopped is now a button.** JARVIS starts an Ollama the
  user already installed. Launching a program someone chose to install is
  not installing one, and that state was otherwise a dead end for anyone
  who did not know Ollama is a background service.
* **The model suggestion is computed from this machine's memory**, not a
  fixed string. Unknown memory falls back to the smallest model —
  conservative costs a little quality; wrong the other way costs a
  machine that swaps itself to a standstill.
* **Ready means it answered.** A short prompt is sent to the model and
  real generated text must come back. A running server and a file on disk
  prove neither that the model loads nor that it generates, and both fail
  in ways that would otherwise first appear mid-conversation.

The page states the boundary rather than hiding it, because a missing
action a user cannot explain looks like a bug.

**To lift the restriction**, `docs/rc2-continuation.md` records three
options and a recommendation. It requires changing `CLAUDE.md`, the
threat-model row and the AST test deliberately and visibly.

---

## Defect 6: the voice

Kokoro 82M on ONNX Runtime is the normal voice; Windows' own natural
voices are second; the old SAPI5 voice is reached only when neither can
run.

Verified against the model rather than assumed:

* The ONNX interface was **read from the pinned model** — `input_ids`
  int64[1, seq], `style` float32[1, 256], `speed` float32[1] →
  `waveform` float32[1, N] at 24 kHz.
* The phoneme→token table was **read from the model's own
  `tokenizer.json`**. The published Kokoro symbol list is longer and
  numbered differently; using it would have sent this model IDs meaning
  other sounds.
* All four pinned voice-pack SHA-256 digests and the model digest were
  re-downloaded and re-verified.
* Real inference runs in the test suite whenever the model is present,
  end to end through this project's own normaliser and G2P.

Measured on a shared 4-core Xeon container — **not** the reporting
machine's Ryzen 7 2700X, and these figures must not be quoted as if they
were:

| | |
|---|---|
| Model | `model_quantized.onnx`, 92,361,116 bytes |
| Voice packs | 522,240 bytes each, four British male voices |
| Sample rate | 24 kHz mono |
| Real-time factor | ≈1.6–1.8× (slower than real time on that hardware) |

That is why synthesis is sentence-by-sentence with playback consuming the
generator lazily: the first sentence is heard while the rest is still
being made, and Stop has somewhere to take effect.

**Licences**, all permissive, none copyleft: ONNX Runtime MIT, numpy
BSD-3-Clause, the Kokoro model and voices Apache-2.0 (downloaded on
request against pinned digests), and the lexicon under CMU's own licence,
shipped verbatim and deliberately **not** labelled with an SPDX
identifier because its text is not literally BSD-2-Clause.

A GPL defect was found and fixed in the process: the release candidate
shipped `pyttsx3`'s espeak ctypes bindings — a loader for a GPL library —
because `collect_all()` copies every `.py` in a collected package into
`_internal/` as loose data. Excluded from both the file lists and the
module graph, and enforced against the real installed tree in CI.

---

## The installer

Built from `87fa49f`, run
[`31478481754`](https://github.com/Dado211207/jarvis-windows-ai-assistant/actions/runs/31478481754)
— **every step green**, including the first compile of the new WebView2
setup code.

| | |
|---|---|
| Filename | `JARVIS-Setup-v0.2.0-rc1-x64.exe` |
| Version reported by the running app | `0.2.0-rc1`, asserted against `app.__version__` |
| Artifact | `JARVIS-Windows-Installer`, id `9096596700`, 98,873,132 bytes (zip) |
| Artifact digest (the zip) | `sha256:d1b6e1389ef95d3e15919e588220891d187c7ee0513a09bd50eaae12777de3b1` |
| Download | https://github.com/Dado211207/jarvis-windows-ai-assistant/actions/runs/31478481754/artifacts/9096596700 |
| Test logs | `JARVIS-Installer-Test-Logs`, id `9096594096` |

**The `.exe`'s own SHA-256 is not quoted here on purpose.** The build
computes it and writes it beside the installer as
`JARVIS-Setup-v0.2.0-rc1-x64.exe.sha256`, which ships inside that
artifact. The line that prints it sits in a part of the job log this
session could not reach, and a digest copied from anywhere other than
the artifact verifies nothing. Read it from the `.sha256` file after
extracting, and check it against the `.exe`:

```powershell
Get-FileHash .\JARVIS-Setup-v0.2.0-rc1-x64.exe -Algorithm SHA256
Get-Content .\JARVIS-Setup-v0.2.0-rc1-x64.exe.sha256
```

The build is unsigned, so SmartScreen will warn. That is expected and is
not worked around: no certificate exists, and faking one would be worse
than the warning.

---

## What CI actually proved

Run [`31393740442`](https://github.com/Dado211207/jarvis-windows-ai-assistant/actions/runs/31393740442),
commit `5fe04ce`, every step green, ending `ALL CLEAN-INSTALL CHECKS PASSED`
— and re-run in full on the final commit `87fa49f`
([`31478481754`](https://github.com/Dado211207/jarvis-windows-ai-assistant/actions/runs/31478481754)):

* **Ten cold start/quit cycles** against the real installed application.
  Each asserted the desktop reported ready, the process was gone,
  `/health` stopped answering, the port was released (by a real
  connection attempt — a *bind* can succeed against a busy port on
  Windows), and no `JARVIS.exe` or JARVIS-started `msedgewebview2.exe`
  was left behind. Ready times 7.2 s–17.6 s.
* **Ten restart cycles**, each asserting the server child's pid changed,
  the previous one no longer existed, and `/health` answered from the new
  one. 3.5 s–5.8 s each.
* **The licence policy against the real packaged tree** — 17 tests.
* **The pinned CMU licence and lexicon bytes preserved byte-for-byte**
  through the installer.
* **Uninstall preserving user data by default**, reinstall over preserved
  data, and uninstall with `/DELETEDATA=yes` actually removing it.

Test totals on the final commit are recorded in
`docs/rc2-continuation.md`; the suite runs with no retries, and no
assertion was weakened or skipped to obtain a pass.

---

## Not verified, and not claimed

1. **Push-to-talk has never touched a physical microphone.** The engine
   ships and the chain is fixed, verified via mocked adapters and browser
   E2E with a fake media device. Real-hardware verification remains a
   manual step.
2. **The WebView2 bootstrap branch is not exercised by CI**, because CI
   runners already have the runtime. What CI proves is that setup still
   succeeds with the step present.
3. **The Windows natural-voice tier has never run.** `winsdk` is now
   installed for packaging so the tier *can* be selected, which it could
   not before; it has not been exercised on a machine with the projection
   present.
4. **Upgrading from an older installed version is not tested.** Reinstall
   over an existing install and both uninstall modes are. A true upgrade
   test needs two installer versions built, which this pass did not do —
   flagged rather than approximated with a same-version reinstall.
5. **Nobody has heard the voices except through generated files.** Four
   WAV samples were produced and handed over; `bm_george` remains the
   default on the strength of matching the stated brief, not on a
   listening decision.
6. **Real-time factor on the reporting machine is unknown.** Only the CI
   container's figures were measured.

---

## Manual acceptance checklist

Install from the artifact, then:

1. Setup completes without showing a terminal, a Python path or a
   localhost URL.
2. First run asks for a name and an Anthropic API key, and nothing else.
   A wrong key is rejected with a reason that names the problem.
3. JARVIS opens in its own window, not a browser tab.
4. Tray → Restart returns to a working window; Task Manager shows one
   JARVIS and no orphaned `msedgewebview2.exe`.
5. Tray → Quit leaves no `JARVIS.exe` and no `msedgewebview2.exe`, and
   the app starts again immediately afterwards.
6. Voice page → install the neural voice; progress, then Test voice
   speaks in a British male voice that does not sound robotic.
7. Voice page → teach it your name, hear it, save it, and confirm a
   reply pronounces it correctly.
8. Settings → Local AI reports the true state of Ollama on the machine
   and its next step is one you can act on without a terminal.
9. Uninstall from Settings → Apps. Data survives. Reinstall and confirm
   your settings are still there. Uninstall again choosing to delete
   data, and confirm `%LOCALAPPDATA%\JARVIS` is gone.
10. **Hold the microphone button on the Chat page and speak.** This is
    the one item on this list that no automated run has ever covered.
