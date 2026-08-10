# RC2 repair pass — state of the work

Written so this can be picked up exactly where it stands, by a session
that starts with no memory of it. Every claim here is either verified or
labelled as not verified; nothing is described as done because it was
written.

Branch: `claude/jarvis-safe-command-center-v2`. PR #15, draft, open,
unmerged, private repository.

---

## One decision is needed before RC2-F can be built

**Local AI (reported defect 4) is blocked on a conflict, not on effort.**

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
`350fd82`, `2a6f7c4`, `9633dbe`, `c491e1a`, `a5be0ab`, `d3d14b2`.

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

**Not yet run.** These were pushed in `a5be0ab` and `d3d14b2`; the
Windows Installer job for them had not reported when this was written.
Their results must be read before either is described as passing.

---

## Not done

| Item | State |
|---|---|
| **RC2-F Local AI** | Blocked on the decision above. Nothing written. |
| **RC2-A WebView2 bootstrapping in the installer** | The native-window and second-launch behaviour is fixed and confirmed by the owner. The installer does not yet bundle or bootstrap the WebView2 runtime, and there is no repair action. |
| **RC2-D push-to-talk on real hardware** | The engine ships and the chain is fixed; verified via mocked adapters and browser E2E with a fake media device. **Never verified against a physical microphone.** Do not claim otherwise. |
| **RC2-G upgrade path** | Reinstall-over-existing and both uninstall modes are covered by phases B and C. Upgrading from an *older installed version* is not exercised. |
| **Voice WAV artefacts per voice** | Not produced. `app/voice/audio.py::write_wav` exists; nothing calls it to emit per-voice samples for review. |
| **`bm_george` as default** | Chosen as the default already, but **not** on the strength of listened-to samples. Nobody has heard these voices; the choice is currently arbitrary and should be confirmed by ear. |
| **Windows natural voices (WinRT)** | `app/voice/winrt_voices.py` is written and degrades honestly when the projection is absent. It has **not** been exercised on a Windows machine with the projection installed, and `winsdk` is not in any requirements file — so in practice this tier is currently always unavailable and the chain falls to SAPI5. |
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
