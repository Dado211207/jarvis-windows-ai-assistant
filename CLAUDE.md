# JARVIS — Claude Code Instructions

## Project purpose

JARVIS is a local-first Windows AI assistant built in Python. This file governs
how Claude Code sessions should work on this codebase.

## Architecture rules

- **Keep it modular.** Never consolidate unrelated logic into one file. Each concern
  lives in its own module: tools in `app/desktop/`, routing in `app/core/router.py`,
  etc.
- **No giant files.** If a file exceeds ~200 lines of logic, ask whether it should
  be split.
- **Register, don't hard-code.** Add new tools via the `ToolRegistry`; do not add
  elif chains to `router.py` or `brain.py`.

## AI provider rules (non-negotiable)

- **One provider contract.** All generation goes through `app/core/ai/`.
  Providers are constructed with a `ProviderConfig`; they never read
  global settings, and they never raise a raw SDK exception past their
  own boundary — only a `ProviderError` carrying an `ErrorCategory`.
- **The failure a user reads must be the failure that happened.** A rate
  limit, an expired key, an unreachable local server and a timeout are
  four different problems with four different fixes. Never collapse them
  into one message, and never tell someone to add an API key when they
  already have one.
- **Never claim a provider or model that was not detected.**
  `POST /providers/select` refuses a provider that is not available and
  an Ollama model the local instance does not report, and the refusal
  names what *is* installed.
- **JARVIS downloads an AI model only when a person presses the button,
  and only from one module.** This rule used to read "JARVIS never
  downloads an AI model"; the product's owner reversed it, deciding that
  someone who wants local AI should be able to get it from a button
  rather than a set of instructions. What replaced it:
  `/api/pull` may be called from `app/core/local_ai_models.py` and
  nowhere else, and `model_puller.start()` may be reached only from the
  session-token-protected `POST /local-ai/pull` — both enforced by tests
  that walk the AST of every module under `app/`. `GET /local-ai/plan`
  names the source, publisher, licence, size and this machine's free
  space *before* anything is fetched, and fetches nothing itself.
  Nothing downloads on startup, on a status read, or as a side effect of
  anything else.
- **The Ollama runtime is installed only after its signature is
  verified.** `app/core/local_ai_install.py` downloads Ollama's own
  installer from a host-pinned HTTPS URL, refuses a redirect off that
  host, verifies the Authenticode signature names Ollama
  (`app/core/authenticode.py`), records the SHA-256, and deletes the file
  rather than running it if any of that fails. There is no "continue
  anyway". Ollama's installer runs visibly, never silently.
- **JARVIS never takes ownership of an Ollama it did not install.** An
  existing installation is used as it is and never reinstalled over;
  whether JARVIS installed it is recorded (`ollama_installed_by_jarvis`)
  so the uninstaller can tell the two cases apart.
- **Anthropic chat never depends on local AI.** Local AI failing, being
  skipped, or never being set up leaves the rest of the product exactly
  as it was.
- **Ollama is loopback-only.** There is deliberately no setting for a
  remote Ollama host; that would turn a local-first assistant into one
  that ships conversations to a machine configured once and forgotten.
- **Conversation history is bounded and privacy-gated.** The last few
  turns are replayed so a follow-up question makes sense; while privacy
  mode is on, a request carries only the message just typed.
- **`/chat/stream` is not a second dispatch path.** It asks
  `router.find_route()` first and executes a matched command through the
  ordinary policy-gated path. No tool is reachable through it that
  `POST /command` could not reach, and the approval gate applies
  identically.

## Coding Workspace rules (non-negotiable)

Coding Workspace is a **separate, explicitly-entered mode** for working on
the user's own code projects. See `docs/coding-workspace-architecture.md`
for the trust boundaries and `docs/desktop-capability-roadmap.md` for what
was deliberately left out.

- **The ordinary assistant gains nothing from its existence.** Coding
  capabilities live in `app/coding/registry.py`, are never added to
  `app.core.tool_registry.registry`, and are not reachable through
  `router.find_route()`. `tests/test_coding_isolation.py` asserts both,
  plus an AST walk proving no module under `app/coding/` imports the
  global registry. Off until the user adds a project.
- **`app/coding/workspace.py` is the only way a path enters the feature.**
  It re-canonicalises the root on every call, compares component-wise
  rather than by string prefix (`/a/b-evil` is not inside `/a/b`), and
  refuses link escapes, device paths, alternate data streams, reserved
  device names and UNC. A second path-checking routine anywhere is a
  defect — that is how `canonical_root()` and `resolve()` drifted apart
  once already.
- **A protected file is never read, and the check runs before the read.**
  The list is `PROTECTED_FILENAMES/SUFFIXES/DIR_COMPONENTS/PATTERNS`, and
  `protected_summary()` is what the UI renders, so the page cannot
  describe a protection the code does not enforce. No secret value may
  reach model context, a log, a diff, a screenshot or a task record.
- **The model never executes anything.** A provider returns a string; it
  is parsed and validated against `app/coding/schema.py`'s closed,
  discriminated union with `extra="forbid"` before anything looks at it.
  An invented tool name and a `skip_approval` field are both validation
  errors. The injection defence is structural, not semantic — do not
  replace it with prompt wording.
- **Repository content is untrusted, including `package.json`.** A
  project's declared script is AUTO only after its *body* is screened for
  blocked programs, and an entry whose body cannot be read fails closed.
  A repository does not get to write its own permission slip.
- **`shell=True` never, and no command line is ever built.** argv only.
  `BLOCKED_PROGRAMS` is refused at every approval level; a user cannot
  approve `powershell`. Install commands disclose the registry, and
  report licence and size as unknown rather than querying the registry to
  find out — that would be a network request made because the user is
  being asked whether to permit one.
- **Every child gets an allowlisted environment**, so no `npm install`
  postinstall script ever sees `ANTHROPIC_API_KEY`. A key that is dropped
  is logged, never silently discarded.
- **No Git verb that can destroy work.** No `reset --hard`, no forced
  checkout, no `clean`, no history rewrite, no push, no branch deletion,
  no remote change. Isolation is a worktree on a task branch; if that is
  not safe, JARVIS stops and explains rather than continuing, and working
  in place is an explicit choice the user makes, never a default.
- **The user's own changes are recorded before anything starts** and are
  labelled as theirs in every diff. Undo checks each file against the
  hash JARVIS last wrote and skips one the user has edited since.
- **A preview is loopback-only, owned, and truthfully reported.**
  "Running" means the owned process is alive *and* the endpoint answers.
  A port somebody else is using is neither adopted nor killed.
- **"Not checked" is never reported as zero.** A browser check that did
  not run reports `None` and says why. Writing `0` after looking at
  nothing is the defect `browser_qa.py` exists to prevent.
- **Nothing is pushed, merged, deployed or cloned in this version**, and
  `GET /coding/status` publishes that list rather than leaving it implied.
- **No test may reach a package registry or a real service.**
  `tests/test_coding_agent.py` carries an autouse guard that turns an
  accidental `npm install` into a failure — it was added because one
  actually happened.

## Preferences store rules (non-negotiable)

- **`app/core/preferences.py` is an allowlist, not a settings store.**
  Only `STORABLE_KEYS` may be written; anything else is refused. It must
  never become a general "write any setting from the browser" mechanism.
- **Never a credential.** API keys live in the OS credential store
  (`app/core/credentials.py`). A plain JSON file in AppData is the wrong
  place for a secret.
- **A saved choice wins over the environment variable**, which supplies
  the starting default. The reverse gives a control that silently does
  nothing on a machine where the variable happens to be set.

## Capability-honesty rules (non-negotiable)

These exist because the installed release candidate, asked "answer me
with your voice", replied that it had no text-to-speech and recommended
Windows Narrator, NaturalReader and Google Docs. Nothing was broken
except the prompt.

- **The model is told what this installation can do, per request.**
  `app/core/capabilities.py` snapshots the real state — active speech
  engine and voice, the Speak-responses setting, push-to-talk, local AI,
  desktop actions — and `build_system_prompt()` appends it. Never cached:
  a voice that finished installing two minutes ago is one the model has
  to know it has. Never a hardcoded list.
- **JARVIS never recommends another program for something it does
  itself.** SYSTEM_PROMPT rule 8 names the three it actually offered.
- **A request to speak is a deterministic route, not a judgement call.**
  "answer me with your voice", "say that again", "read this aloud" and
  their neighbours reach `speak_last_reply` (`app/voice/speak_reply.py`).
- **An explicit one-off utterance is not gated on the always-speak
  switch.** `output_enabled` answers "speak every reply automatically";
  `/voice/speak` is gated on it and `/voice/speak-once` deliberately is
  not, exactly as `tts_test` has always behaved. Still one flag, not two.
- **One utterance at a time.** Every path stops what is playing before
  starting.
- **An unavailable capability reports the cause and the step that fixes
  it.** "Voice input — Not set up" over six accurate rows and a
  reinstall suggestion that would not have helped is the failure this
  replaces; `app/voice/input_state.py` holds the ten states.

## Cloud voice rules (non-negotiable)

ElevenLabs is an **optional** premium tier. See
`docs/clean-room-and-voice-identity.md` for the voice brief and the
clean-room decision behind it.

- **Local stays the default, and stays sufficient.** Kokoro, Windows
  natural voices and SAPI5 all keep working with no key, no network and
  no account. `ENGINE_ORDER` is the *local* chain and ElevenLabs is
  deliberately not in it: a tier that costs money and sends text to a
  third party may only ever be chosen by name.
- **The key lives in Windows Credential Manager, in its own entry**
  (`credentials.ELEVENLABS_USERNAME`), never in preferences, the
  database, `.env`, a log, a diagnostic or an event. No endpoint returns
  it; the UI learns only whether one exists.
- **One pinned host, no redirects, bounded everything.**
  `app/voice/elevenlabs.py` talks to `api.elevenlabs.io` over HTTPS with
  `follow_redirects=False`, connect and read timeouts, a capped response
  body and a content-type check before a byte is treated as audio. There
  is no endpoint that fetches an audio URL supplied by anyone.
- **Privacy mode blocks it completely** — no text is sent, and the
  refusal says so rather than quietly using a different voice.
- **A fallback is always visible.** If the local voice covers for the
  cloud one, the reason is reported. A silent fallback would make an
  expired key and an exhausted quota both sound like success.
- **Nothing is created, cloned or billed automatically.** No voice
  design, no cloning, no credits spent without a button press. Nothing
  ElevenLabs-related runs during installation, onboarding, startup,
  tests or CI, and no test may ever call the real API.
- **The voice is original.** It does not imitate or clone any actor,
  performer or copyrighted character, and this project never claims
  otherwise.

## Double-clap activation rules (non-negotiable)

The product owner asked for one hands-free convenience and granted a
deliberate, extremely narrow exception to the blanket ban on continuous
listening in the Safety rules below. The exception is this feature and
nothing else. `app/voice/clap.py` and `app/ui/static/clap-processor.js`
carry the full reasoning; these are the lines that may not move.

- **A transient counter, not a listener.** The detector computes
  root-mean-square and peak amplitude per 128-sample block, and nothing
  else. No FFT, no frequency analysis, no wake word, no speech
  recognition, no transcription. Enforced by a test that reads the
  worklet's source.
- **Nothing is recorded, stored or sent.** The worklet's state is six
  scalars, overwritten every block. No buffer, no history, no file, no
  upload. Its only output is `{type: "clap-pair"}` — a message with no
  payload, because there is nothing in it that would be safe to carry.
- **A clap can only show the window.** Activation goes through
  `app/launcher/attention.py`, whose entire message is the existence of a
  marker file. `POST /voice/clap/activate` takes **no request body** and
  never will: a microphone must not be able to name an action.
- **Off by default, and gated three times server-side** — the stored
  preference, privacy mode (which `app/core/privacy.py` always said a
  future listener would have to honour), and a refractory interval.
  Server-side because a page left open before the feature was switched
  off elsewhere must not still be able to act.
- **No new audio dependency.** The detector runs in the page that is
  already open, on the microphone stream the level meter already uses.
  JARVIS still has no native audio *input* dependency — no PortAudio, no
  PyAudio, no sounddevice — and playback is still stdlib `winsound`.
- **It is tested against real audio, in a real browser.**
  `tests/test_clap_detection.py` plays synthesised claps, speech, a hum
  and silence through Chromium's fake capture device and asserts that
  only a genuine pair activates anything. Never replace it with a mock:
  a state machine asserted against itself proves nothing about whether
  speech sets it off.
- **Privacy mode releases the microphone, it does not merely relabel
  it.** The failure this replaces is a build that repainted the privacy
  indicator and left the capture running. `setPrivacyBlocked(true)` in
  `app/ui/static/clap-controller.js` tears down synchronously: every
  track stopped, the source and worklet disconnected, the `AudioContext`
  closed. Stopping the tracks is what turns the Windows
  microphone-in-use indicator off; closing the context alone does not.
- **One controller owns the microphone.** `clap-controller.js` is a
  single state machine with nine states and one decision point
  (`reconcile()`); nothing else may call `start()` or `teardown()`.
  Scattered booleans are what produced the defect above, so do not
  reintroduce them — and do not split the file: the cohesion is the
  reason it holds.
- **Suspension is reference-counted.** Speech, push-to-talk, the
  microphone test and calibration each take a named reason. Two reasons
  in and one out leaves the listener suspended. Every path releases its
  reason on success, failure and cancellation alike.
- **The microphone dropdown is the one shared choice**, not a
  diagnostic. It reaches `getUserMedia` as `deviceId: {exact: …}`. A
  missing device falls back to the system default **and says so**;
  claiming the chosen device is active when it is not is the failure
  mode this exists to prevent. There is never more than one clap stream.
- **Calibration is bounded, local and opt-in.** It owns the microphone
  for its duration and releases it on success, timeout, cancel,
  navigation, privacy and quit. The per-onset scalars exist only behind
  the worklet's `calibrate` guard, never leave the page, and nothing is
  saved without an explicit Save — clamped server-side to `SAFE_BOUNDS`.
  The attack and sustained-sound thresholds are never calibratable.
- **The tray may say "On" only while a page has recently proved a live
  microphone.** `clap.py::tray_label()` composes the line and refuses
  "On" for a stale report; the page re-sends its report well inside
  `LISTENER_FRESH_SECONDS` so the reverse dishonesty — "Microphone
  unavailable" while it is plainly listening — cannot happen either.
- **The lifecycle assertions are on resources, never on flags.**
  `tests/test_clap_controller.py` asserts against real
  `MediaStreamTrack`s, `AudioContext`s, worklet nodes, listeners and
  timers in a real browser. A test that reads a boolean would have passed
  against the broken build.

## Memory secret rules (non-negotiable)

These exist because `memory add my key is sk-ant-…` stored the key
verbatim, in plaintext, in a file that lives on the user's disk until
they delete it.

- **The check runs before the write, never after.** `app/core/secret_guard.py`
  is consulted in `app/core/memory.py::add_memory()` *and* again in
  `db/database.py::Database.add_memory()`, which is the only place a
  memory row is ever inserted. The value must never reach the database,
  so there is never anything to purge.
- **A second INSERT INTO memories anywhere else is a defect**, enforced
  by a test that greps `app/` and `db/` for one.
- **The detected value is never echoed.** `find_secret()` returns a
  label ("an Anthropic API key"), never the matched text, and
  `SecretRejected` carries the same label. A guard that quotes what it
  caught puts the secret in the API response, the event stream and the
  log — the thing it exists to prevent.
- **Rejection, never redaction.** A memory is refused, not rewritten.
  Storing "my key is ***" would leave the user a memory they never
  wrote, saying something they did not say.
- **Ordinary sentences must still be storable.** "Remind me to change my
  password on Friday" contains no secret and is saved. The bar for
  refusal is a credential-shaped string, or a credential noun with a
  value attached — not the mere mention of one.
- **`app/core/redaction.py` is not this.** It redacts tool inputs headed
  for a log line, the audit trail or a WebSocket event, and never runs
  on the memory write path. Both exist; neither replaces the other.

## Legacy data rules (non-negotiable)

- **The v0.1 database is only ever read.** `app/core/legacy_migration.py`
  never moves, modifies or deletes it — not even after a successful
  import. Somebody who wants it gone deletes it themselves.
- **Never overwrite data that is already here**, and never merge two
  histories. An *empty* destination is fine to replace: that is a schema
  `create_tables()` made a moment ago. Rows are not.
- **Look, do not search.** Every candidate is one `exists()` call
  against a documented or default location. No globbing, no `os.walk`,
  no disk scanning — enforced by a test.
- **Copy to a temporary name, then rename.** An interruption must never
  leave a half-written file where JARVIS expects its database.
- **Validate before trusting**: a SQLite integrity check *and* a look
  for the tables a JARVIS database has, so an unrelated `.db` file in a
  candidate location is never adopted as somebody's history.
- **Decide once.** A marker records the outcome — including refusals —
  so repeated launches cannot duplicate anything or re-run the work.
- **Nothing here may raise.** It runs on the startup path of a windowed
  build with no console, where an unhandled exception becomes a modal
  dialog nobody can dismiss. Optional data is not worth that.

## Process lifecycle rules (non-negotiable)

These exist because a WebView2 process outlived JARVIS on cycle 2 of the
installer's ten-cycle lifecycle test while cycle 1 — and an entire
sibling run of the identical commit — passed. See
`docs/webview2-lifecycle-defect.md`.

- **JARVIS terminates only processes it can *prove* are descendants of a
  process it started.** Targets come from walking down from a PID this
  launcher spawned (`app/launcher/process_tree.py::capture_descendants`)
  and from nowhere else. No `process_iter`, no `taskkill /IM`, no name
  matching — an unrelated Edge or WebView2 the user is browsing in is
  never ours to touch.
- **A PID is not an identity.** Windows recycles PIDs, and cleanup holds
  its targets across a grace period. Every target is a
  `ProcessIdentity` (PID *plus* creation time), re-verified immediately
  before it is signalled; a mismatch is reported as `pid_reused` and the
  process is left alone. An identity captured without a creation time is
  `inaccessible` and also left alone — unverifiable is not the same as
  ours.
- **Every escalation ends in a bounded wait, including after `kill()`.**
  "Killed" must mean the process is gone, not that `kill()` did not
  raise. Shutdown stays bounded by construction — one terminate grace
  plus one kill grace, whatever the processes do — because JARVIS must
  always be able to close.
- **Cleanup returns a structured report and never raises.** Six
  outcomes: `already_gone`, `terminated`, `killed`, `still_alive`,
  `inaccessible`, `pid_reused`. A survivor is a logged warning naming
  the process, never silence. Shutdown completes even if cleanup itself
  fails.
- **Diagnostics carry no paths.** PID, image name, parent PID and
  booleans only. A full Windows path contains the account name, and
  these records go into a log file.
- **Capture before the poll, not after**, and expand each captured
  identity to its own live descendants at cleanup time. WebView2 starts
  helper processes lazily; one born in the last interval before the
  window child exits is exactly the one that gets orphaned.
- **The lifecycle test asserts on identities, and its wait may never
  grow to cover a leak.** `scripts/test_clean_install.py` waits for the
  exact captured processes to reach a terminal state within a bound
  close to the product's own cleanup worst case. Never raise it to make
  something pass: a leaked process never exits, so a longer wait cannot
  turn a real leak green — it can only turn a slow one invisible.

## Uninstall rules (non-negotiable)

- **`app/core/ownership.py` is the manifest.** "Remove everything JARVIS
  owns" is only a promise if there is a list, and the list distinguishes
  what setup installed from what the application created while running.
- **The application removes its own things**, via
  `JARVIS.exe --uninstall-cleanup`, because only it knows how the API key
  was stored. An installer guessing at a Credential Manager target name
  is how an uninstall leaves a secret behind while reporting success.
- **Data and credentials survive an ordinary uninstall.** `--purge-data`
  is a choice, never an inference. The sign-in shortcut goes either way:
  it points at an executable that is about to stop existing.
- **Shared Windows components are never removed** (WebView2, the Visual
  C++ runtime), nor Ollama and its models — even when JARVIS installed
  it — nor anything in `Documents\JARVIS_Notes`.

## Phase 3 TTS rules (non-negotiable)

- **Output only.** Phase 3 TTS is text-to-speech output. No microphone input,
  no speech-to-text, no always-listening behavior, no wake word — ever.
  (v0.2 added push-to-talk input as a separate, explicitly user-triggered
  feature in `app/voice/stt.py`; the TTS engine itself still never
  captures audio.)
- **One flag decides whether JARVIS speaks:** `tts_service.output_enabled`.
  Every surface reads it — the `speak on`/`speak off` commands, the Voice
  page toggle, the `/voice/speak` gate, `/voice/status` and the CLI. Two
  flags is how the desktop app ended up never speaking at all.
- **The speech gate is server-side.** A page left open before speech was
  switched off elsewhere must not be able to make JARVIS talk.
- **Approval prompts are never read aloud.** They are to be read and
  decided on.
- **TTS failures must never crash the app.** All pyttsx3 errors are caught and
  logged; the app continues normally without audio.
- **Tests must mock the TTS engine.** No test may play real audio or require
  audio hardware. Use `unittest.mock.patch("pyttsx3.init")` or equivalent.
- **TTS is disabled by default.** `JARVIS_TTS_ENABLED=false` in `.env.example`.
  Users must opt in explicitly.
- **No cloud TTS.** Only local/offline engines (pyttsx3 / OS SAPI/espeak).
  Do not add cloud TTS APIs without explicit design review.

## Phase 2 AI rules (non-negotiable)

- **No autonomous tool execution by Claude.** The AI may only respond with text.
  It must not trigger tools, run commands, or take system actions on its own.
- **Deterministic routes always take priority.** The router's ROUTES list is matched
  first; only unrecognised commands fall through to `Brain.generate_response()`.
- **Never expose the API key.** Settings fields, API responses, and log output
  must never include `ANTHROPIC_API_KEY` or any `sk-` token.
- **No real API calls in tests.** All Anthropic SDK calls must be mocked via
  `unittest.mock.patch("anthropic.Anthropic")`.
- **Local fallback is always available.** When `ANTHROPIC_API_KEY` is absent or the
  API call fails, Brain returns a polite local message — never an unhandled exception.
- **System prompt is immutable.** The JARVIS system prompt in `app/core/system_prompt.py`
  defines the AI's constraints and must not be weakened by user input or tool additions.

## Safety rules (non-negotiable)

- **Never commit secrets.** `.env` is gitignored. `ANTHROPIC_API_KEY` and any other
  credentials live only in `.env`, never in source code or config files.
- **No destructive PC actions without approval.** Any tool that deletes files,
  modifies system settings, or sends data externally must use
  `PermissionLevel.APPROVAL_REQUIRED` or `PermissionLevel.BLOCKED`.
- **No direct dangerous PowerShell execution.** `subprocess` calls must use explicit
  argument lists (never `shell=True` with untrusted input). Anything shell-like
  that could cause data loss requires approval.
- **No surveillance tools.** Do not implement keyloggers, clipboard sniffers,
  webcam capture, or continuous screen recording.
- **API stays local.** FastAPI binds to `127.0.0.1` only. Never change to `0.0.0.0`
  without explicit user approval and a security review.

## Development workflow

- **Small PRs.** One feature or fix per pull request. Do not bundle unrelated changes.
- **Run tests before reporting done.** `pytest` must pass before marking any task
  complete. Run `python -m compileall app` as well.
- **Branch naming.** Use `feat/`, `fix/`, `chore/`, or `docs/` prefixes.
- **Never merge without user approval.** Always open a draft PR and wait.

## Phase 4 dashboard rules (non-negotiable)

- **No API key in templates.** Jinja2 templates must never render `ANTHROPIC_API_KEY`
  or any `sk-` token. All sensitive settings stay server-side only.
- **textContent only.** All dynamic text inserted into the DOM via JavaScript must
  use `textContent` (never `innerHTML`) to prevent XSS.
- **Dashboard calls existing API only.** The browser calls `POST /command`,
  `GET /health`, `GET /logs`, `GET /memory`, `GET /voice/status`, etc.
  It does not directly invoke tools or bypass the permission system.
- **Static files bundled with PyInstaller.** Use `--add-data` for both
  `app/ui/templates` and `app/ui/static` so the dashboard works in the `.exe` build.
- **No external CDNs.** All CSS and JS is served locally from `/ui/static/`.
  No remote fonts, no analytics, no tracking scripts.
- **API binds to 127.0.0.1 only.** The dashboard is not accessible from other
  devices on the network. Never change the bind address.

## Phase 5 approval system rules (non-negotiable)

- **No action may bypass the approval gate.** Any tool registered with
  `PermissionLevel.APPROVAL_REQUIRED` must never execute through `registry.execute()`.
  Execution only happens via `registry.execute_approved()` after explicit user confirmation.
- **No arbitrary command execution.** Only allowlisted tools registered in the
  `ToolRegistry` may be executed. Do not add shell passthrough or generic exec tools.
- **Approval-required commands must return a pending action preview.** The router
  must intercept `APPROVAL_REQUIRED` tools in `_dispatch()` and create a `PendingAction`
  instead of calling `registry.execute()`. The `CommandResponse` must include
  `requires_approval=True` and `pending_action_id`.
- **Confirmed actions must be logged.** After execution via the confirm endpoint,
  write to `action_logs` with status `success` or `failure`.
- **Cancelled actions must never execute.** Status transitions are final. A
  `cancelled`, `expired`, or `executed` action cannot be re-confirmed. The cancel
  endpoint must write status `blocked` to `action_logs`.
- **Pending actions are in-memory and expire after 10 minutes.** This is intentional.
  Stale approvals from before a restart are never executed. Document this clearly
  in any UI that surfaces pending actions.
- **No secrets in pending action payloads.** Action previews served to the browser
  must not include `ANTHROPIC_API_KEY`, `.env` values, or any `sk-` tokens.
- **Confirmation goes through the tool registry.** `execute_approved()` calls the
  tool handler directly but the handler itself must not bypass OS security or
  perform privileged operations without user intent.

## Phase 6 safe actions rules (non-negotiable)

- **Allowlisted apps only.** `open_app` must only launch executables in `APP_ALLOWLIST`
  or URI handlers in `_URI_APPS`. No arbitrary paths, no shell=True.
- **Allowlisted folders only.** `open_folder` must only open folders in the hardcoded
  map (home subdirs + JARVIS root). No `..` traversal, no arbitrary paths.
- **Safe URL schemes only.** `open_website` must reject `file:`, `javascript:`, `data:`,
  `powershell:`, `cmd:`, `vbscript:`, and any non-http/https scheme. Parse with
  `urlparse` BEFORE prepending `https://` to detect existing dangerous schemes.
- **Notes confined to JARVIS_Notes.** `create_note` writes only to
  `~/Documents/JARVIS_Notes/`. Filenames are sanitised; paths are validated with
  `note_path.resolve().relative_to(NOTES_DIR.resolve())` before writing.
- **Network info is local-only.** `get_network_info` uses only `socket` — no HTTP
  requests, no external DNS, no port scanning.
- **All Phase 6 tools are SAFE permission level.** None require approval or are blocked.
  No Phase 6 tool deletes files, modifies settings, or sends data externally.

## Phase 7 action rules (non-negotiable)

- **Notes are addressed by filename, never by path.** `read_note` refuses
  a name containing a separator or `..` rather than sanitising it, and
  re-checks containment after resolving, so a symlink planted in the
  notes folder cannot read outside it. Notes are still never deleted.
- **Locking is the only session action that will ever exist.** Sign out,
  restart, sleep and shut down all end running programs and can lose
  unsaved work in other applications; locking cannot. See
  `app/desktop/session.py`, whose test asserts nothing else was added.
- **Process information is a snapshot on request.** Nothing is sampled in
  the background, recorded, or stored — that would be the monitoring this
  file's Safety rules forbid.

## Phase 7 dashboard rules (non-negotiable)

- **Sidebar layout only.** The dashboard uses a fixed 240 px sidebar replacing the
  old top-nav bar. Do not revert to top-nav or add a second navigation structure.
- **CSS design system via custom properties.** All colours, radii, and shadows are
  defined as `--var` tokens in `:root`. Do not hardcode colour values elsewhere.
- **No external fonts or CDN resources.** The CSS `style.css` must not contain any
  `https://` or `http://` URL. All assets are served from `/ui/static/`.
- **textContent only — still enforced.** All dynamic text injected via JS must use
  `textContent`. The `innerHTML` property is permanently forbidden.
- **Topbar status indicators are read-only.** The topbar health/brain dots are updated
  from `GET /health` data only. They do not expose config values or API keys.
- **Progress bars are cosmetic only.** CPU/RAM bars display live `GET /system` data.
  They do not control anything.
- **Chat suggestions are client-side only.** Suggestion chips populate the input field
  only; they do not auto-submit or bypass the normal send flow.

## v0.2 safe command center rules (non-negotiable)

v0.2 ("Safe Voice Command Center and Windows Action Runtime") is an
infrastructure milestone that runs across the phase numbering below, not
a replacement for it — Phase 8/9/10's planned scope (OCR, browser
automation, smart home) is unchanged. It added the pipeline future tools
should be built on top of; see `docs/audit-v0.2.md` and
`docs/THREAT_MODEL.md` for the full picture.

- **The policy engine is the only place risk decisions are made.**
  `app/core/policy.py::evaluate()` decides auto-execute / require-approval
  / deny from a tool's `RiskLevel`. Do not add a second, ad-hoc
  risk/permission check elsewhere — extend `evaluate()` or a tool's
  declared `RiskLevel` instead of duplicating the decision in a route.
- **New tools should declare a `RiskLevel` and, when they take arguments,
  an `input_model`.** A tool that omits them still works (see
  `policy.py::risk_for()`'s conservative legacy mapping from
  `PermissionLevel`) but new tools should declare them explicitly rather
  than rely on that fallback.
- **The runtime state machine is the only source of truth for "what is
  JARVIS doing right now."** Use `app/core/runtime_state.py`'s
  `runtime.transition()` (or `try_transition()` from a request-handling
  code path, which must never raise — see its docstring). Do not track
  state ad hoc elsewhere.
- **The `action_lifecycle` audit trail is additive.** It never replaces
  or gates `app/core/pending_actions.py`'s live approval queue; it
  records what happened. Execution must never depend on the audit write
  succeeding.
- **The WebSocket stream (`/ws/events`) is read-only.** It broadcasts
  typed events; it must never accept a command or an action approval.
  Command submission and approval stay on the REST endpoints.
- **No raw tool input reaches a log line, the audit trail, or a
  WebSocket event unredacted.** Use
  `app/core/redaction.py::redact_params()` before persisting or
  publishing anything derived from tool kwargs.
- **`read_clipboard` is the only clipboard capability, and it is
  SENSITIVE / approval-required, permanently.** No clipboard writing, no
  history, no polling or monitoring — it must never grow into the
  clipboard-sniffer this file's Safety rules already forbid.

## Phase guide

| Phase | Status      | Scope |
|-------|-------------|-------|
| 1     | ✅ Done      | Foundation: CLI, router, tool registry, permissions, SQLite, FastAPI |
| 2     | ✅ Done      | Claude AI integration, natural-language fallback via Anthropic SDK |
| 3     | ✅ Done      | TTS voice output (pyttsx3, local/offline, output-only, no microphone) |
| 4     | ✅ Done      | Local browser dashboard: FastAPI + Jinja2 + vanilla JS |
| 5     | ✅ Done      | Action approval system: pending actions, confirm/cancel, Actions UI |
| 6     | ✅ Done      | Safe Windows actions: URL opener, folders, notes, disk, network, battery |
| 7     | ✅ Done      | Professional UI/UX polish: sidebar layout, design system, metric cards |
| 8     | Planned     | Screen intelligence / OCR (on-request only, explicit user permission) |
| 9     | Planned     | Browser automation (approval-gated, no autonomous browsing) |
| 10    | Planned     | Smart home, health, trading integrations |

> **v0.2** (infrastructure, not a numbered phase): Safe Voice Command
> Center and Windows Action Runtime — runtime state machine, typed
> tool/risk/policy contract, persisted `action_lifecycle` audit trail,
> real-time WebSocket event stream, a new `read_clipboard` tool, a safe
> error envelope, enforced tool-execution timeouts, per-session REST/WS
> mutation protection, a minimum privacy mode, push-to-talk voice input,
> an automated Playwright/axe browser-test suite, and a real Windows CI
> smoke job. Wake-word/always-listening voice, a complete visual
> redesign, an Ollama adapter, a full memory/retention redesign, and
> real-microphone/real-Windows-hardware verification of push-to-talk
> (verified so far only via mocked adapters and browser E2E with a fake
> media device) remain deferred — see `docs/audit-v0.2.md`,
> `docs/THREAT_MODEL.md`, and the PR description for the exact scope and
> honest gaps.

> **Coding Workspace** (infrastructure, not a numbered phase): a separate
> explicitly-entered mode for working on the user's own code projects —
> containment boundary, protected-path engine, patch-based editing with
> stale-base detection, a three-tier command policy, process-tree
> ownership, Git worktree isolation that never touches uncommitted work,
> a closed proposal schema as the prompt-injection defence, a loopback
> preview with real browser checks, and its own page. Pushing, pull
> requests, merging, deployment, remote cloning and general web browsing
> are all deliberately excluded — see `docs/desktop-capability-roadmap.md`
> for each one's reason. Browser checks need Playwright, which the
> packaged build does not carry; it reports that rather than reporting
> zero problems.

> **Desktop release-candidate pass** (also not a numbered phase, and
> deliberately using its own numbering in the PR): the packaged Windows
> desktop application. Three-process shell (tray parent, server child,
> native window child) with authenticated IPC, an Inno Setup installer
> and uninstaller, a first-run wizard, Settings and Diagnostics pages, a
> Home overview, the AI provider pipeline above (streaming, stop,
> conversation reset, selectable Anthropic/Ollama), spoken replies that
> actually work in the desktop app, and the Phase 7 actions above.
> Wake-word voice, OCR, browser automation and the Phase 8–10 scope
> below are all still out.

## Do NOT implement in this repo (ever, without explicit separate design review)

- Password extraction (browser, OS, WiFi)
- Remote control / AnyDesk automation
- Email sending without approval flow
- Mass file deletion
- Network scanning or port scanning
- Anything that could be used for surveillance

## Testing

**Run the suite the way the installer build runs it.** `scripts/build-installer.ps1`
sets `JARVIS_LOG_LEVEL=WARNING` and a temp `JARVIS_DB_PATH` before
`pytest`, so a test that quietly assumes the default log level passes
locally and fails the Windows Installer job. Worse, it can pass *both*
while proving nothing: three redaction tests logged at INFO, wrote an
empty file, and two of them asserted only "the secret is not in the
file". Before calling a change done:

```bash
JARVIS_LOG_LEVEL=WARNING JARVIS_DB_PATH=/tmp/jarvis_gate.db pytest
```

```bash
# Run all tests
pytest

# Compile-check all modules
python -m compileall app db

# Start CLI
python -m app.main

# Start API
python -m app.api.server
```
