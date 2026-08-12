# RC3 — the three defects the physical Windows test found

The release candidate built from `1558c62` was installed on a real
Windows machine as an upgrade over the previous RC. The automated suite
was green. The installed product was not. This is what was wrong, what
was actually causing it, and how each one is verified now.

Nothing in here is a plan. Every item below is implemented on this
branch, and every claim names the test that holds it down.

---

## 1. Voice input was absent from the installed application

**What was seen.** "Voice input — Not set up. The local speech engine
isn't available in this installation. Reinstalling JARVIS should restore
it." Diagnostics: microphone permission not asked, one input device,
speech runtime not installed, no model, no model location, no last check.

**Root cause.** `packaging/jarvis.spec` listed four of faster-whisper's
own hard dependencies — `av`, `tokenizers`, `huggingface_hub`, `tqdm` —
in `_OPTIONAL_PACKAGES`. `faster_whisper/__init__` imports
`faster_whisper.audio`, which imports `av` at package-import time, so the
installed application's very first `import faster_whisper` raised
ImportError. The optional list prints "skipping" and carries on; nobody
read it.

Every automated check passed because every one of them imported
`faster_whisper` in the source tree, where pip had installed it.

**Fixed by**

- `packaging/jarvis.spec`: all six declared hard dependencies are
  required. A required package that cannot be collected now stops the
  build instead of printing a warning.
- `app/launcher/selftest.py`: `JARVIS.exe --selftest` asks the *installed
  executable* whether each runtime it claims actually loads, and
  `--deep` goes further — see §4.
- `app/voice/input_state.py`: ten states, each with its own next step,
  replacing one sentence that recommended a reinstall which would not
  have helped. Three of the ten (permission not asked, permission
  refused, no input device) are decided in the browser and overlaid on
  the server's answer using the same state names.
- A failed transcription is remembered — the message only, never audio
  and never a transcript — so push-to-talk stops looking like a button
  that does nothing.

**Verified by** `tests/test_packaging_spec.py` (the dependency test fails
against `1558c62`), `tests/test_selftest.py`,
`tests/test_voice_input_state.py`, and phases F and G of
`scripts/test_clean_install.py`, which run against the real installer.

---

## 2. JARVIS said it had no voice

**What was seen**, verbatim:

> **User:** answer me with your voice
> **JARVIS:** I appreciate the request, Dado, but I don't have
> text-to-speech capabilities built into this interface right now. I can
> only communicate through text.

followed by recommendations for Windows Narrator, NaturalReader and
Google Docs.

**Root cause.** Nothing was broken. JARVIS has a complete local TTS
stack — a neural British voice, a Windows natural-voice tier and a
classic tier — and the model was never told any of it existed. Asked a
question about itself with no information, a model answers from general
knowledge about chat interfaces: plausibly, and wrongly.

**Fixed by**

- `app/core/capabilities.py`: a per-request snapshot of the real state —
  which engine is active and under which voice, whether Speak responses
  is on, push-to-talk, local AI, desktop actions — appended to the system
  prompt. Never cached; never a hardcoded list. Generated from booleans
  and a fixed set of engine names, so it cannot carry an injected
  instruction.
- `SYSTEM_PROMPT` rule 8: never guess at your own capabilities, and never
  recommend a third-party program for reading text aloud. The three it
  actually offered are named.
- `app/voice/speak_reply.py`: "answer me with your voice", "say that
  again", "read this aloud" and their neighbours are deterministic
  routes now. The request never reaches the model's opinion of itself.
- A speaker button on every JARVIS message, with stop, replay and
  interruption; a "Speak replies" switch in the chat toolbar writing the
  same saved setting as the Voice page; one utterance at a time.
- `POST /voice/speak-once` for an explicit request, deliberately not
  gated on the always-speak switch — pressing the button is the request.
  `/voice/speak` keeps its gate for the automatic path.

**Verified by** `tests/test_capabilities.py` (7 of 20 fail against the
parent commit), `tests/test_speak_intent.py` (31 of 56 fail),
`tests/test_chat_speech_controls.py`, and six real-browser tests.

---

## 3. Local AI setup was unusable for a normal Windows user

**What was seen.** JARVIS could describe the problem accurately and then
handed over a set of instructions.

**Root cause.** A deliberate rule — "JARVIS never downloads an AI model"
— that the product's owner has now reversed. Someone who wants local AI
should be able to get it from a button.

**What replaced the rule**, since a reversal without containment is just
a removal:

- `/api/pull` may appear in `app/core/local_ai_models.py` and nowhere
  else; `model_puller.start()` may be reached only from the
  session-token-protected `POST /local-ai/pull`. Both enforced by AST
  tests over every module under `app/`.
- `GET /local-ai/plan` answers "what would this download?" — source,
  publisher, licence, size, this machine's free space — and fetches
  nothing. A test asserts that asking makes no network call and runs no
  process.
- Ollama's installer is fetched from a host-pinned HTTPS URL, a redirect
  off the allowlist is refused, its Authenticode signature is verified
  against Windows' own trust store *and* required to name Ollama, and
  its SHA-256 is recorded. Any failure deletes the file. There is no
  "continue anyway". The check talks to `wintrust.dll` and `crypt32.dll`
  through ctypes, so there is no command line to smuggle a path into.
- Ollama's own installer runs visibly, under Windows' elevation prompt.
- An Ollama that was already on the machine is used as it is, never
  reinstalled over, and recorded as not ours.
- The model pull reports progress, cancels without losing what arrived,
  resumes on retry, and names the fix for a full disk, a corrupted
  layer, a wrong name and a dropped connection separately. It is not
  complete until the model has actually generated text.

**Verified by** `tests/test_local_ai_setup.py` (39 tests) and the updated
AST tests in `tests/test_ai_providers.py`.

---

## 4. Proving it through the installed executable

The lesson from defect 1 is that a source-tree test proves the build
machine has a package. It says nothing about the artifact a person was
sent.

`JARVIS.exe --selftest --deep`, run by phase G of the clean-install test:

1. Both models are installed **through the installed application's own
   download screens** — the same endpoints a button press reaches, with
   the same consent previews. If those screens do not work, this fails.
2. The installed executable synthesises a sentence with the real Kokoro
   model and writes a real WAV, checking the duration and that the
   samples are not silence. Loading `onnxruntime` proves the runtime is
   bundled; it does not prove the voice makes a sound.
3. The same executable feeds that WAV to the real speech recogniser and
   checks the words come back.

Without `--deep` those two checks are named as skipped. A check nobody
ran is never reported as a pass.

The clean-install step in `.github/workflows/windows-installer.yml`
carries no `if:` of its own: the path gate decides whether the job runs,
and once it does, every installed-product check runs.

---

## What is still not covered

Stated plainly rather than left to be discovered.

- **A real microphone.** Push-to-talk is exercised with a fake media
  device in Chromium and with real audio synthesised by JARVIS itself.
  Nobody has spoken into a physical microphone in an automated run, and
  no automated run can.
- **A real Ollama install.** The download, the signature check and the
  model pull are unit-tested against patched boundaries. CI does not
  download 700 MB of somebody else's installer and run it, and the
  Authenticode verification cannot execute on a Linux dev machine at
  all.
- **Windows natural voices (WinRT).** Present as a selectable tier and
  reported honestly when unavailable; a machine with them installed has
  not been part of an automated run.
- **The desktop shell on real hardware.** Ten start/stop cycles and ten
  restarts run on a CI runner, which is a Windows machine but not
  *your* Windows machine.
