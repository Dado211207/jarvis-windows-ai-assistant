# RC2 repair pass — state of the work

Written so this can be picked up exactly where it stands, by a session
that starts with no memory of it. Every claim here is either verified or
labelled as not verified; nothing is described as done because it was
written.

Branch: `claude/jarvis-safe-command-center-v2`. PR #15, draft, open,
unmerged, private repository.

---

## The local-AI decision, and how it was resolved

**Resolved: the rule stands.** Instructed to apply the safest choice
consistent with the existing instructions, RC2-F was built *without*
auto-installing Ollama and *without* downloading models. The conflict
and the options are kept below because reversing the decision later
should be a deliberate act with the reasoning in front of whoever makes
it.

**Local AI (reported defect 4) conflicted with a standing rule.**

The defect asks that JARVIS "auto-install the runtime, select a
hardware-appropriate model, and download it with progress, cancel, retry
and a checksum".

`CLAUDE.md` forbids exactly that, in a rule marked non-negotiable:

> **JARVIS never downloads an AI model.** `/api/pull` is never called
> from anywhere in this codebase — enforced by a test that walks the AST
> of every module under `app/`.

It is not a loose convention. It is enforced by
`tests/test_ai_providers.py`, restated in `docs/THREAT_MODEL.md`, and
paired with the loopback-only rule that exists so a local-first assistant
cannot be pointed at someone else's machine.

Both positions are coherent. Downloading a multi-gigabyte model on a
user's behalf is a large, slow, silent-by-default action, and the rule
exists to stop it happening without intent. Equally, an assistant with a
"Local AI" mode that cannot install local AI is not much use to a
non-technical owner.

**This needs an explicit decision** — it is the owner's call, not one to
make by quietly deleting a security rule or by quietly ignoring the
requirement. Three workable shapes:

1. **Keep the rule; make the guidance excellent.** Detect Ollama, detect
   installed models, and when neither is present, say precisely what to
   install and offer a link. No download from JARVIS. Smallest change,
   keeps every existing guarantee, and does not satisfy the defect as
   written.
2. **Relax the rule to "never downloads *without explicit consent*".**
   Reuse the pattern already built and proven for the neural voice
   (`app/voice/kokoro/install.py`): show size, source and licence first,
   verify a pinned SHA-256, write to a staging directory, move into place
   only after verification, and allow cancel and retry. This satisfies
   the defect and keeps the property the rule was protecting. It requires
   rewriting the CLAUDE.md rule, the threat-model row, and the AST test,
   deliberately and visibly.
3. **Split the difference:** JARVIS may pull a model through a *local*
   Ollama that the user installed themselves, but never installs the
   Ollama runtime. Removes the largest, least reversible part
   (installing a service) while making model choice usable.

My recommendation is (2), because it is the same trade already accepted
for the voice model and the machinery to do it safely exists and is
tested. But it changes a documented security posture and should not
happen without the owner saying so.

Everything else below is independent of this decision.

---

## Done and verified

Commit range on the branch, oldest first:
`350fd82`, `2a6f7c4`, `9633dbe`, `c491e1a`, `a5be0ab`, `d3d14b2`,
`5cc78fe`, `9e639a6`, `5fe04ce`.

### The espeak defect in the installed tree (`350fd82`)

The release candidate shipped `_internal/pyttsx3/drivers/espeak.py` and
`_espeak.py` — a loader for a GPL library — inside a Windows-only
application that can never select that driver. `collect_all()` passes
`include_py_files=True`, so it copies every `.py` in a collected package
into `_internal/` as loose data on top of the module in the archive.

Dropped from the collected file lists and excluded from the module graph.
**Verified on real Windows CI**: run `31387695988`, job "Windows
Installer / windows-latest", step "Licence policy — inspect the real
packaged tree" — success, against the actual installed tree.

### The neural voice (`2a6f7c4`, `9633dbe`, `c491e1a`)

Kokoro 82M on ONNX Runtime as the normal voice; Windows natural voices
second; SAPI5 only when neither can run (`app/voice/engines.py`).

Verified rather than assumed:

- All four pinned voice-pack SHA-256 digests in `assets.py` were
  re-downloaded and re-verified, not just the default one.
- The ONNX interface was **read from the pinned model**, not from
  documentation: `input_ids` int64[1, seq], `style` float32[1, 256],
  `speed` float32[1] → `waveform` float32[1, N] at 24 kHz.
- The phoneme→token table was **read from the model's own
  `tokenizer.json`** (`app/voice/kokoro/tokens.py`). The published Kokoro
  symbol list is longer and numbered differently; using it would have
  sent this model IDs meaning other sounds. 115 tokens, IDs sparse to
  177, and a test asserts the set matches what the G2P validates against.
- Both pinned SHA-256 digests in `assets.py` were re-downloaded and
  re-checked and match exactly.
- **Real inference runs in the test suite** whenever the model is
  present, end to end through this project's own normaliser and G2P
  (`tests/test_kokoro_engine.py`). Skips loudly, with the install path,
  when it is not.

`scripts/make_voice_samples.py` writes one WAV per installed voice
through the real path, so the voice can be chosen by ear and re-checked
whenever the normaliser or G2P changes.

Measured, on the machine that ran it — a shared 4-core Xeon @ 2.80 GHz
container, **not** the owner's Ryzen 7 2700X:

| | |
|---|---|
| Model | `model_quantized.onnx`, 92,361,116 bytes |
| Voice pack | 522,240 bytes each, four British male voices |
| Sample rate | 24 kHz mono |
| Real-time factor | ≈1.7× (slower than real time on this hardware) |

That RTF is why synthesis is sentence-by-sentence with playback
consuming the generator lazily: the first sentence is heard while the
rest is still being made. It also gives Stop somewhere to take effect.
**Figures on the owner's hardware are unknown and must not be quoted
from this table.**

Two defects found while doing it:

- `onnxruntime` **cannot be loaded twice in one process**. Probing
  availability with a bare `import` meant anything that dropped it from
  `sys.modules` killed the neural voice for the rest of the process,
  permanently. The module reference is now held and the question
  answered once.
- A test asserting "unavailable when pyttsx3 is missing" only ever
  passed because no other engine existed. With the model installed it
  correctly reports available. Both availability tests now pin which
  tier they are about.

Licences: ONNX Runtime MIT, numpy BSD-3-Clause, model and voices
Apache-2.0 (downloaded on request against pinned digests), lexicon under
CMU's own licence shipped verbatim and **not** labelled with an SPDX
identifier. Recorded in `app/voice/kokoro/assets.py::LICENCE_MANIFEST`
and served at `GET /voice/licences`, so the page cannot drift from what
ships.

### Custom pronunciations (`9633dbe`)

`app/voice/pronunciations.py`. Exists for the first-run name: no general
dictionary contains "Vukoje", and without this JARVIS spells its user's
name out at them forever. An entry can be heard before it is saved, and
one using symbols the voice cannot say is refused at entry rather than at
speech. The app offers the setting when a name would be spelled out and
stays quiet when it would not — proposing a pronunciation nobody asked
for would be inventing how someone's name sounds.

A real isolation defect was found here: the store lives beside the
preferences file, and `conftest.py`'s autouse fixture redirected only the
preferences module, so the suite was writing to the developer's real user
data. Fixed, with a test that fails if it regresses.

### Lifecycle verification (`a5be0ab`, `d3d14b2`)

`scripts/test_clean_install.py` now runs five phases. Two are new:

- **Phase D** — ten cold start/quit cycles against the **real installed
  application**. Each cycle asserts the desktop reported ready, the
  process is gone, `/health` stopped answering, the port was released
  (by a real connection attempt, not a bind — a bind can succeed against
  a busy port on Windows), and neither a `JARVIS.exe` nor a
  JARVIS-started `msedgewebview2.exe` was left behind.
- **Phase E** — ten restarts through `LauncherSupervisor.restart()`, the
  real method the tray item calls, each asserting the server child's pid
  changed, the previous one no longer exists, and `/health` answers from
  the new one.

**Both phases now pass against the real installed application**, run
[`31393740442`](https://github.com/Dado211207/jarvis-windows-ai-assistant/actions/runs/31393740442),
commit `5fe04ce`, job "Windows Installer / windows-latest" — every step
green, ending `ALL CLEAN-INSTALL CHECKS PASSED`.

Phase D, ten cold start/quit cycles against the installed `JARVIS.exe`.
Each line is the real log:

    OK: cycle 1  - ready in  8.0s, exited cleanly, port released,
                   no JARVIS or WebView2 process left behind
    ... cycles 2-9, ready in 7.4s to 17.6s ...
    OK: cycle 10 - ready in  7.2s ...
    OK: 10 consecutive start/quit cycles left nothing behind

Phase E, ten restarts, each proving replacement rather than survival:

    OK: cycle 1  - new server pid=7780 healthy in 3.5s, previous generation gone
    ... cycles 2-9, healthy in 4.9s to 5.8s ...
    OK: cycle 10 - new server pid=1620 healthy in 5.7s, previous generation gone
    OK: 10 restart cycles, each replacing the previous runtime completely

Getting there took one real fix. **Phase D failed on its first cycle the
first time it ran** (run `31392641519`):

    FAILED: Cycle 1: 1 WebView2 process(es) started by JARVIS
            outlived it (pids [8340]).

That is reported defect 8 — Quit leaving a JARVIS-owned WebView2 process
behind — reproduced automatically. Phase A had passed on the same run,
because it only ever asked whether `JARVIS.exe` itself had exited.

The cause was in `WindowProcess.stop()`, which asked for the window
child's descendants *after* `poll()` confirmed it had already exited.
`process_tree.py`'s own docstring states the rule it was breaking: once
the parent is gone, the relationship identifying its descendants is gone
with it, so a capture taken afterwards walks a dead PID and returns
nothing. The kill path captured correctly; the graceful path — the one
taken every ordinary time somebody chooses Quit — did not, so it cleaned
up nothing, every time.

Fixed in `5fe04ce` by capturing before anything is asked to stop,
re-capturing while the child is still alive (WebView2 starts helpers
lazily), and terminating that accumulated set on every exit path. Three
regression tests cover it; all three were confirmed to fail against the
previous code.

### The installer built from `5fe04ce`

| | |
|---|---|
| Filename | `JARVIS-Setup-v0.2.0-rc1-x64.exe` |
| Version reported by the running app | `0.2.0-rc1` (asserted against `app.__version__`) |
| Artifact | `JARVIS-Windows-Installer`, ID `9065124959`, 98,861,744 bytes (zip, containing the .exe and its `.sha256`) |
| Run | https://github.com/Dado211207/jarvis-windows-ai-assistant/actions/runs/31393740442 |
| Artifact link | https://github.com/Dado211207/jarvis-windows-ai-assistant/actions/runs/31393740442/artifacts/9065124959 |
| Test logs | `JARVIS-Installer-Test-Logs`, ID `9065122227` |

The installer `.exe`'s own SHA-256 is computed by the build and written
beside it as `JARVIS-Setup-v0.2.0-rc1-x64.exe.sha256`, which ships inside
that artifact. **It is deliberately not quoted here**: it sits in a part
of the job log this session could not reach, and a digest copied from
somewhere other than the artifact is worth nothing. Read it from the
`.sha256` file after downloading, and check it against the `.exe`.

Also confirmed in the same run: the licence-policy suite (17 tests)
against the real packaged tree, the pinned CMU licence and lexicon bytes
preserved byte-for-byte through the installer, uninstall preserving user
data by default, reinstall over preserved data, and uninstall with
`/DELETEDATA=yes` actually removing it.

---

## Not done

| Item | State |
|---|---|
| **RC2-F Local AI** | Built within the rule (`57cf0a3`): four states, start-an-installed-Ollama, a model chosen from this machine's memory, and Ready only after real generated text. Auto-install and model download remain deliberately absent — see the decision section above, which is now a record of what was decided rather than a question. |
| **RC2-A WebView2 bootstrapping in the installer** | Done (`beb0282`). Setup detects the runtime against the same registry key the app uses and installs it via Microsoft's bootstrapper when absent, from `PrepareToInstall` so silent installs take the same path. A failure never aborts the install. **The download branch is not exercised by CI** — runners already have the runtime. |
| **RC2-D push-to-talk on real hardware** | The engine ships and the chain is fixed; verified via mocked adapters and browser E2E with a fake media device. **Never verified against a physical microphone.** Do not claim otherwise. |
| **RC2-G upgrade path** | Reinstall-over-existing and both uninstall modes are covered by phases B and C. Upgrading from an *older installed version* is not exercised. |
| **`bm_george` as default** | Samples for all four voices were produced and sent to the owner. The default stays `bm_george` **until the owner says otherwise** — the choice is theirs to confirm by ear, and has not been confirmed yet. |
| **Windows natural voices (WinRT)** | `winsdk` is now in `requirements-windows.txt` and collected as an *optional* package, so the tier can be selected at all — it previously could not, making the adapter unreachable. It has still **not** been exercised on a Windows machine with the projection installed. |
| **Installer artefact for the final commit** | Not produced or hashed yet. |
| **Final report** | Not written. |

---

## Things worth not re-learning

- `collect_all()` copies `.py` files into `_internal/` as data. Anything
  bundled is licence surface whether or not it can ever run.
- `patch.dict(sys.modules, ...)` removes on exit anything first imported
  inside the block. With lazily-imported modules that quietly leaves two
  live copies of a module, and patching the wrong copy makes a test pass
  or fail depending on which test ran first. Patch through the module the
  code under test actually consults.
- On Windows, `SO_REUSEADDR` lets a bind succeed against a port something
  is actively listening on. Test port availability by connecting.
- The tray's popup menu builds its command IDs when it opens, so there is
  no stable ID an external process can post to. Driving Restart from
  outside would require shipping a control path into the product purely
  for tests. Not worth it; the supervisor method is driven directly
  instead and the menu entry is unit-tested.
- Git line-ending translation rewrites hash-pinned files on Windows
  checkout. `.gitattributes` carries `-text` for `docs/licences/**` and
  `app/voice/kokoro/data/**`.
