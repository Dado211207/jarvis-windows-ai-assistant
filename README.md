# JARVIS — Personal Windows AI Assistant

> v0.2: Safe Voice Command Center & Windows Action Runtime — every action is
> classified by risk, policy-gated, and recorded in a persisted audit trail
> before it ever touches your system.

JARVIS is a local Windows AI assistant that brings together PC automation,
memory, system monitoring, voice output, and Claude AI —
all running privately on your machine, never in the cloud unless you choose to
enable it.

---

## v0.2 — Safe Voice Command Center & Windows Action Runtime

This milestone adds the pipeline every command actually flows through, and
makes it visible: **user input → deterministic routing → risk
classification → policy decision → optional approval → execution → audit
record → real-time dashboard update.** Nothing here replaces the Phase 5
approval system below — it is built on top of it.

**What's implemented and tested:**

- **Runtime state machine** (`app/core/runtime_state.py`) — one
  authoritative "what is JARVIS doing right now" model
  (`booting`/`standby`/`listening`/`thinking`/`awaiting_approval`/`executing`/`speaking`/`error`/`offline`),
  every transition validated and turned into an event.
- **Typed tool contract + policy engine** (`app/core/policy.py`,
  `app/core/models.py`) — a five-tier `RiskLevel`
  (`read_only`/`reversible`/`sensitive`/`destructive`/`blocked`) drives one
  policy decision (`auto_execute`/`require_approval`/`deny`); tools may
  declare a Pydantic `input_model` validated before the handler ever runs.
- **Persisted action audit trail** (`app/core/action_lifecycle.py`, new
  `action_lifecycle` table) — every proposed action, across every risk
  tier, gets a durable record: tool, risk, policy reason, redacted input,
  timestamps, result, duration. Additive migration — your existing
  `memories`/`conversations`/`action_logs` data and the live approval
  queue are untouched.
- **Real-time WebSocket event stream** (`GET /ws/events`) — typed,
  read-only events (runtime state changes, action proposals, approval
  changes, results) with Origin validation and a bounded-backoff
  reconnect. The topbar shows connected / reconnecting / offline; the
  Actions page updates live when a pending action changes anywhere, not
  just from a button click on that page.
- **New tool: `read_clipboard`** — reads the current text clipboard.
  Always SENSITIVE / approval-required; content is shown once to the
  approving caller and never appears in a log line, the audit trail, or a
  WebSocket event.
- **Security fixes** — the dashboard's CORS `allow_origins` previously
  used glob patterns (`"http://127.0.0.1:*"`) that Starlette's
  `CORSMiddleware` never actually matches (it compares by exact string);
  this is now a real, working allowlist shared with the new WebSocket's
  own Origin check.
- **Genuine concurrency tests** — the existing double-execution guard is
  now also proven under real multi-threaded contention, not only
  sequential double-calls (`tests/test_concurrency.py`).

**Deferred (named honestly, not silently skipped):** voice input
(speech-to-text, wake word), a full visual redesign, an automated
Playwright/axe browser-test suite, a Windows CI smoke job, and an Ollama
provider adapter. See `docs/audit-v0.2.md` for the baseline audit and
`docs/THREAT_MODEL.md` for exactly what is and is not protected.

---

## Action Approval System (Phase 5)

Before JARVIS can execute higher-risk actions, it pauses and asks you to confirm.

### How it works

1. You send a command that maps to an approval-required action (e.g. `clear logs`).
2. JARVIS does **not** execute it. Instead it creates a **pending action** with a preview.
3. The response includes `requires_approval: true` and a `pending_action_id`.
4. You review the action on the **Actions page** or via API and click **Confirm** or **Cancel**.
5. Confirm → executes through the existing tool registry and permission system; result is logged.
6. Cancel → marks the action as cancelled; nothing is executed; logged as `blocked`.
7. Unconfirmed actions expire after **10 minutes** and are never automatically executed.

### Approval API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/actions/pending` | List all pending approval actions |
| GET | `/actions/{id}` | Get one action by ID |
| POST | `/actions/{id}/confirm` | Confirm and execute |
| POST | `/actions/{id}/cancel` | Cancel (never executes) |

### Safety guarantees

- **Cancelled actions are never executed.** Status transitions are final.
- **Executed actions cannot run twice.** Idempotent-safe by design.
- **Confirmation goes through the tool registry.** No permission bypass.
- **Pending actions are in-memory.** They reset on app restart (documented limitation).
- **All events are logged.** Confirmed actions write `success` or `failure`; cancelled actions write `blocked`.
- **No secrets in action previews.** API key and `.env` values never appear in action payloads.

---

## Browser Dashboard (Phase 4)

Open **http://127.0.0.1:5555/ui/** in any browser while JARVIS API is running.

| Page | URL | Description |
|---|---|---|
| Dashboard | `/ui/` | Live system health — CPU, RAM, AI brain, TTS status |
| Chat | `/ui/chat` | Send commands and questions; approval cards shown inline |
| Actions | `/ui/actions` | Review, confirm, or cancel pending approval actions |
| Logs | `/ui/logs` | Recent command and tool execution history |
| Memory | `/ui/memory` | Browse and search saved memories |
| Voice | `/ui/voice` | TTS status and controls |
| Help | `/ui/help` | Quick start guide and command reference |

Every page connects to the v0.2 real-time event stream (`GET /ws/events`) —
the topbar shows a live connected / reconnecting / offline indicator and the
current runtime state.

The dashboard is **local-only** — it never exposes your API key to the browser
and only binds to `127.0.0.1`. No external CDNs, no analytics, no tracking.

---

## What Phase 1–6 includes

| Feature | Status |
|---|---|
| Modular project architecture | ✅ |
| CLI chat interface (`jarvis> `) | ✅ |
| Command router (deterministic) | ✅ |
| Tool registry with permission levels | ✅ |
| Permission system (safe / approval / blocked) | ✅ |
| App launcher (allowlist-only) | ✅ |
| Screenshot tool | ✅ |
| System status (CPU, RAM, disk, battery) | ✅ |
| SQLite memory (add / search) | ✅ |
| FastAPI local API (127.0.0.1:5555) | ✅ |
| Structured logging to file | ✅ |
| Pydantic settings / `.env` config | ✅ |
| Claude AI brain (natural-language fallback) | ✅ |
| Conversation history in SQLite | ✅ |
| Local fallback when no API key | ✅ |
| Windows installer scripts | ✅ |
| Pytest test suite | ✅ |
| TTS voice output (pyttsx3, local/offline) | ✅ |
| Voice CLI commands (speak on/off/test/status) | ✅ |
| Voice API endpoints (`/voice/status`, `/voice/speak`, `/voice/stop`) | ✅ |
| Local browser dashboard (7 pages, dark UI) | ✅ |
| Dashboard chat page (calls `POST /command`) | ✅ |
| Dashboard memory browser | ✅ |
| Dashboard TTS controls | ✅ |
| Action approval system (pending / confirm / cancel) | ✅ |
| Approval API (`/actions/pending`, `/confirm`, `/cancel`) | ✅ |
| Actions dashboard page with confirm/cancel cards | ✅ |
| Chat inline approval cards | ✅ |
| Approval events logged (`success` / `failure` / `blocked`) | ✅ |
| Safe URL opener (http/https only; dangerous schemes blocked) | ✅ |
| Open JARVIS dashboard from CLI | ✅ |
| Safe folder opener (allowlisted: downloads, documents, desktop, notes…) | ✅ |
| File note creator (`create note <text>` → `~/Documents/JARVIS_Notes/`) | ✅ |
| Dedicated disk space command | ✅ |
| Dedicated network info command (local only, no scanning) | ✅ |
| Dedicated battery/power status command | ✅ |
| Expanded app allowlist (file explorer, settings) | ✅ |

## Approval-required commands

| Command | What it does | Risk |
|---|---|---|
| `clear logs` | Clears all action log entries from the database | Medium |
| `read clipboard` (or `clipboard`, `show clipboard`) | Reads the current text clipboard contents | Medium (v0.2: SENSITIVE) |

## What is NOT included (current alpha)

The following are **not** in the current release. Some are planned for later phases with explicit user confirmation and safety controls; others are permanently excluded.

**Planned later (with explicit user permission and safety controls):**
- Screen intelligence / OCR — on user request only (Phase 8)
- Browser automation with approval gate (Phase 9)
- Smart home, health and trading integrations (Phase 10)

**Session actions that will not be added:** JARVIS can lock the screen and
nothing else. Sign out, restart, sleep and shut down all end running
programs and can lose unsaved work in other applications; locking cannot.
See `app/desktop/session.py`.

**Permanently excluded:**
- Wake-word speech recognition. Push-to-talk voice input **is** included:
  one recording per button press, started and stopped by the user. Optional
  double-clap activation is separate: when explicitly enabled, it keeps the
  microphone open to measure sound levels only. It never recognises words,
  records audio, or sends microphone data anywhere, and Privacy Mode stops it.
- Email sending without an approval flow
- AnyDesk / remote control
- Any network exposure (API binds to `127.0.0.1` only, never `0.0.0.0`)
- Password / credential extraction
- Keylogging or clipboard surveillance
- Network scanning or port scanning
- Mass file deletion

---

## Requirements

- Python 3.11+
- Windows 10/11 (for app launcher and screenshots; CLI/API work cross-platform)
- **Microsoft Edge WebView2 Runtime**, for the desktop window. It ships
  with Windows 11 and with recent Windows 10, and the installer fetches
  and installs it from Microsoft if it is missing — so this is a
  dependency to know about rather than one to go and get. It is a shared
  Windows component: JARVIS never removes it, even when it installed it.
  The double-clap listener and push-to-talk both run inside that
  WebView2 window, which is why they stop when JARVIS is quit.

---

## Installation

### Windows — the installer

**`JARVIS-Setup-v<version>-x64.exe` is how JARVIS is installed.** It is a
per-user Inno Setup installer: it needs no administrator rights, installs
to `%LOCALAPPDATA%\Programs\JARVIS`, keeps your data separately in
`%LOCALAPPDATA%\JARVIS`, and adds a Start Menu entry. Python is not
required — the runtime is inside the executable.

Run it, then follow the first-run wizard (a preferred name and, if you
want AI replies, an Anthropic API key). Everything else — the voice, the
speech model, local AI — is set up from inside the application when you
choose to.

`docs/WINDOWS_INSTALLER.md` has the full walkthrough, including how to
verify the download's SHA-256 before running it.

> **v0.2.0-rc1 is a release candidate, not a published release.** There
> is no GitHub Release and no tag for it. Builds are produced by the
> **Windows Installer** workflow and attached to that workflow run, which
> requires a signed-in account with read access to this repository and
> expires after 30 days. The build is also **unsigned**, so Windows
> SmartScreen will warn about an unrecognised publisher.

### From source (developers only)

Not the way to install JARVIS. This is for working *on* it — the
installed application above is a packaged build of this same tree.

```bash
git clone https://github.com/dado211207/jarvis-windows-ai-assistant.git
cd jarvis-windows-ai-assistant
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # optional: add ANTHROPIC_API_KEY
```

`installer\JARVIS_SETUP.bat` and `installer\install.ps1` are development
helpers with the same status: they prepare a source checkout on Windows,
and they are not an installation method. Neither are the
`START_JARVIS*.bat` / `SETUP_ENV.bat` scripts in the repository root,
which date from the v0.1 alpha ZIP and only make sense inside a source
tree.

---

## Running JARVIS

### CLI

```bash
python -m app.main
```

You will see:

```
╔══════════════════════════════════════════════════════╗
║  JARVIS — Personal Windows AI Assistant              ║
╚══════════════════════════════════════════════════════╝
jarvis>
```

### Local API

```bash
python -m app.api.server
```

Then open **http://127.0.0.1:5555/docs** for the interactive Swagger UI.

---

## Supported commands

### General

| Command | What it does |
|---|---|
| `help` | List all available tools |
| `status` | Show JARVIS version and config |
| `exit` | Quit JARVIS CLI |

### System info

| Command | What it does |
|---|---|
| `system status` | CPU, RAM, disk, battery summary |
| `disk space` | Show disk usage (used / free / total) |
| `network status` | Show hostname and local IP (read-only, no scanning) |
| `battery status` | Show battery / power status |
| `what time is it` | Local date and time, read from this computer's clock |
| `top processes` | What is using the most memory right now — a snapshot, nothing is recorded |
| `lock screen` | Lock Windows, exactly like Win+L. Nothing closes and no work is lost |
| `screenshot` | Capture screen → `data/screenshots/` |
| `take screenshot` | Same as above |

### Open apps

| Command | What it does |
|---|---|
| `open chrome` | Launch Chrome (Windows, allowlist only) |
| `open notepad` | Launch Notepad |
| `open calculator` | Launch Calculator |
| `open brave` / `open edge` | Launch Brave / Edge |
| `open vscode` | Launch VS Code |
| `open spotify` | Launch Spotify |
| `open discord` | Launch Discord |
| `open file explorer` | Open Windows File Explorer |
| `open settings` | Open Windows Settings |

### Open URLs & dashboard

| Command | What it does |
|---|---|
| `open website <url>` | Open an http/https URL (dangerous schemes blocked) |
| `open dashboard` | Open the JARVIS local dashboard in your browser |

### Open folders

| Command | What it does |
|---|---|
| `open downloads` | Open your Downloads folder |
| `open documents` | Open your Documents folder |
| `open desktop` | Open your Desktop folder |
| `open notes folder` | Open the JARVIS Notes folder |
| `open jarvis folder` | Open the JARVIS project folder |

### Notes & memory

| Command | What it does |
|---|---|
| `create note <text>` | Save a timestamped note to `~/Documents/JARVIS_Notes/` |
| `list notes` | List saved notes, newest first |
| `read note <filename>` | Read one note back (only from the notes folder; a path is refused) |
| `memory add <text>` | Save a note to local SQLite memory |
| `memory search <query>` | Search saved memories |

### Voice

| Command | What it does |
|---|---|
| `speak on` | Speak replies out loud. The choice is remembered across restarts |
| `speak off` | Stop speaking replies |
| `speak status` | Show whether replies are spoken, and the speech engine status |
| `speak test` | Speak a test phrase aloud |
| `stop speaking` | Stop current speech immediately |

### Approval-required

| Command | What it does |
|---|---|
| `clear logs` | Delete all action log entries — requires confirmation |
| `read clipboard` | Read the current text clipboard — requires confirmation (v0.2) |

### API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Status JSON |
| GET | `/health` | Health check |
| POST | `/command` | `{"command": "system status"}` |
| GET | `/tools` | List all registered tools (v0.2: includes `risk` and `input_model` JSON schema) |
| GET | `/memory/search?q=...` | Search memory |
| GET | `/memory` | List recent memories |
| GET | `/logs` | Recent action logs |
| GET | `/actions/pending` | List pending approval actions |
| GET | `/actions/{id}` | Get one pending/resolved action by ID |
| POST | `/actions/{id}/confirm` | Confirm and execute a pending action |
| POST | `/actions/{id}/cancel` | Cancel a pending action |
| GET | `/voice/status` | Whether replies are spoken / engine / available |
| POST | `/voice/output` | `{"enabled": true}` — turn spoken replies on or off; remembered |
| POST | `/voice/speak` | `{"text": "hello"}` — speak text (refused when speech is off) |
| POST | `/voice/stop` | Stop current speech |
| GET | `/voice/stt-status` | Push-to-talk readiness (local speech recognition) |
| POST | `/chat/stream` | `{"command": "..."}` — the chat answer, streamed as server-sent events |
| POST | `/chat/stop` | Stop a generation in progress |
| POST | `/conversation/reset` | Delete stored chat history (leaves the action audit trail alone) |
| GET | `/providers` | Which AI providers were actually detected, and which is selected |
| POST | `/providers/select` | `{"provider": "ollama", "model": "llama3:latest"}` — refuses anything not detected |
| GET | `/diagnostics` | Copyable diagnostic report — never contains a credential |
| WS | `/ws/events` | **(v0.2)** Real-time typed event stream — read-only, optional `?since=<seq>` to resume after a reconnect. See `app/api/ws.py`. |

---

## Configuration

Copy `.env.example` to `.env` and edit as needed:

```env
ANTHROPIC_API_KEY=          # Add your key to enable Claude AI responses
JARVIS_HOST=127.0.0.1       # Never change to 0.0.0.0
JARVIS_PORT=5555
JARVIS_DB_PATH=data/jarvis.db
JARVIS_LOG_LEVEL=INFO

# Phase 2 AI settings
JARVIS_AI_PROVIDER=anthropic
JARVIS_AI_MODEL=            # Leave blank for default (claude-haiku-4-5-20251001)
JARVIS_AI_MAX_TOKENS=250
JARVIS_AI_TIMEOUT_SECONDS=20

# Phase 3 TTS / voice output (local/offline — no cloud API required)
JARVIS_TTS_ENABLED=false    # Set to true to enable voice output
JARVIS_TTS_ENGINE=pyttsx3
JARVIS_TTS_RATE=175         # Words per minute
JARVIS_TTS_VOLUME=1.0       # 0.0 – 1.0
JARVIS_TTS_VOICE=           # Leave blank for system default voice
```

Without an `ANTHROPIC_API_KEY`, JARVIS operates fully in local mode — all
deterministic commands (system status, memory, app launcher, screenshot) work
normally; unrecognised commands receive a polite local fallback message instead
of an AI response.

---

## Development / CI

### Run tests locally

```bash
# Compile-check all modules
python -m compileall app db

# Run full test suite (unit + smoke tests)
pytest
```

### What is tested

| Suite | File | Coverage |
|---|---|---|
| Permissions | `tests/test_permissions.py` | All 3 levels, blocked keywords |
| Router | `tests/test_router.py` | All command patterns |
| Tool registry | `tests/test_tool_registry.py` | Register, execute, error handling, typed input validation |
| Smoke | `tests/test_smoke.py` | Imports, routing, registry, API endpoints |
| Brain / AI | `tests/test_brain.py` | is_configured, AI mock, fallback, DB storage, API |
| Runtime state (v0.2) | `tests/test_runtime_state.py` | Every legal/illegal transition, event publication |
| Policy engine (v0.2) | `tests/test_policy.py` | Full risk → decision matrix, legacy mapping, determinism |
| Redaction (v0.2) | `tests/test_redaction.py` | Sensitive-key masking, truncation |
| Action lifecycle (v0.2) | `tests/test_action_lifecycle.py` | Real SQLite round-trip, idempotency, terminal-state guard |
| WebSocket stream (v0.2) | `tests/test_ws.py` | Origin allowlist, snapshot-on-connect, `?since=` resume |
| Origin allowlist (v0.2) | `tests/test_origin.py` | CORS/WS shared allowlist |
| Clipboard tool (v0.2) | `tests/test_clipboard.py` | Content never in logs/messages, graceful degradation |
| Pipeline integration (v0.2) | `tests/test_pipeline_integration.py` | Real end-to-end dispatch → policy → lifecycle → events |
| Concurrency (v0.2) | `tests/test_concurrency.py` | Real multi-threaded races, not just sequential double-calls |
| Migration compatibility (v0.2) | `tests/test_migration_compatibility.py` | Upgrading an existing pre-v0.2 database preserves data |
| AI providers | `tests/test_ai_providers.py` | Per-cause failure classification, streaming, cancellation, Ollama model honesty, no model download |
| Chat pipeline | `tests/test_chat_pipeline.py` | Honest error messages, bounded history, privacy gating, streaming/stop/reset endpoints |
| Provider selection | `tests/test_provider_selection.py` | Preferences allowlist, precedence, refusing an undetected provider or model |
| Voice output | `tests/test_voice_output.py` | One flag across every surface, server-side gate, approval prompts never spoken |
| Phase 7 actions | `tests/test_phase7_actions.py` | Note containment against real files and symlinks, clock, process snapshot, lock |
| Action history | `tests/test_action_history.py` | Audit trail exposure, filtering, totals, no secret ever served |
| Memory & privacy | `tests/test_memory_privacy.py` | Save/delete from the page, approval-gated bulk clear, stored-data counts |
| About & updates | `tests/test_about_and_updates.py` | Version consistency across three files, no automatic update check, licence notices |
| Security invariants | `tests/test_security_invariants.py` | Every mutating endpoint token-gated, no shell/eval/pickle, no credential literal, approval gate holds |
| Browser + accessibility | `tests/test_playwright_e2e.py` | Real Chromium: streaming, Stop, spoken replies, skip link, heading order, axe |

### CI pipeline

GitHub Actions runs on every **pull request** and every **push to `main`**:

- OS: `ubuntu-latest`
- Python: `3.11`
- Steps: checkout → install `requirements.txt` → `compileall` → `pytest`

Workflow file: `.github/workflows/ci.yml`

> Note: Windows-only tools (app launcher, screenshot) are tested at the
> routing/registration level only in CI. Actual OS calls are not made on Linux.

---

## Builds

Two workflows produce Windows output, and they are not the same thing.

| Workflow | Produces | Who it is for |
|---|---|---|
| `windows-installer.yml` | `JARVIS-Setup-v<version>-x64.exe` + its `.sha256` | **The installer.** What a person installs. |
| `windows-build.yml` | An unpacked `JARVIS\` folder | A developer wanting the frozen tree without installing |

Both attach their output to the workflow run that produced it. Neither
publishes anything.

### Getting the installer

Full walkthrough, including checksum verification: **[`docs/WINDOWS_INSTALLER.md`](docs/WINDOWS_INSTALLER.md)**.

In short — open **Actions → Windows Installer**, pick the run you were
pointed at, download the **JARVIS-Windows-Installer** artifact, and check
its SHA-256 against the bundled `.sha256` before running it.

### Who can download a build

This repository is public; its workflow artifacts are not. GitHub serves
an artifact download only to an account **signed in with read access to
the repository**, from that run's own page, over a short-lived signed
URL. Anonymous visitors cannot fetch it.

Artifacts also **expire** — 30 days for the installer, 14 for the test
logs — so a link to one is worth nothing a month later. That is why no
document here links to a specific build.

### Not a release

There is no GitHub Release and no tag for v0.2.0-rc1, and this pass
creates neither. Builds are **unsigned**, so SmartScreen warns about an
unrecognised publisher — accurately, which is why the checksum step
matters. Signing and an auto-updater are both deliberately out of scope;
see `docs/THREAT_MODEL.md`.

The v0.1 alpha tags (`v0.1.0-alpha` … `v0.1.6-alpha`) are historical.
They predate the installer and shipped a ZIP with batch-file helpers;
`docs/release-process.md` describes that older flow.

---

## Project structure

```
app/
  main.py           — CLI entry point
  config.py         — Pydantic settings
  logging_config.py — Structured logging setup
  core/
    brain.py            — Orchestrator + Claude AI integration
    router.py           — Command → tool mapping; v0.2 pipeline dispatch
    tool_registry.py    — Central tool registry; v0.2 input_model validation
    permissions.py      — Permission enforcement (SAFE/APPROVAL_REQUIRED/BLOCKED)
    policy.py            (v0.2) — Risk classification + the policy engine
    runtime_state.py      (v0.2) — Authoritative runtime state machine
    events.py             (v0.2) — Typed event envelope + in-memory event bus
    action_lifecycle.py   (v0.2) — Persisted action audit trail
    redaction.py           (v0.2) — Sensitive-key masking for logs/audit/events
    pending_actions.py  — Live in-memory approval queue (unchanged by v0.2)
    memory.py           — Memory tool wrappers
    models.py           — Shared Pydantic models (incl. v0.2 RiskLevel, ActionLifecycleStatus)
    system_prompt.py    — JARVIS AI safety constraints
    ai/                 — Provider contract, Anthropic and Ollama implementations
    conversation.py     — History sent to a provider, persistence, and reset
    generation.py       — Live generations, so one can be stopped
    providers.py        — Provider detection: what this machine can really use
    preferences.py      — Small allowlisted store for choices made in the app
    credentials.py      — API key in the OS credential store (never a file)
    diagnostics.py      — The copyable report, built by allowlist
  desktop/
    apps.py         — App launcher (allowlist)
    web.py          — Safe URL opener
    folders.py      — Safe folder opener (allowlisted roots)
    notes.py        — Notes: create, list, read (confined to JARVIS_Notes)
    screenshots.py  — Screenshot tool
    system.py       — System status, clock, memory snapshot (psutil)
    session.py      — Lock the screen; the only session action that exists
    maintenance.py  — Log clearing (approval-required)
    clipboard.py     (v0.2) — read_clipboard (SENSITIVE, approval-required)
  voice/
    tts.py          — Offline speech output (pyttsx3)
    stt.py          — Push-to-talk speech recognition (local, opt-in)
  launcher/         — Desktop shell: tray, server child, native window child
  ui/
    routes.py       — Dashboard pages
    templates/, static/ — Jinja2 + vanilla JS/CSS, no external CDNs
  api/
    server.py       — FastAPI app; CORS + lifespan runtime-state wiring
    routes.py       — Route handlers
    actions.py      — Approval confirm/cancel; v0.2 lifecycle sync
    chat.py         — Streaming chat, stop-generation, conversation reset
    origin.py         (v0.2) — Shared CORS/WebSocket origin allowlist
    ws.py             (v0.2) — GET /ws/events real-time stream
db/
  database.py       — SQLite access layer; v0.2 action_lifecycle CRUD
  migrations.py     — Schema creation; v0.2 additive action_lifecycle table
data/               — Runtime data (gitignored except .gitkeep)
tests/              — Pytest suite (see "What is tested" above)
installer/          — Windows setup scripts
docs/
  audit-v0.2.md     — Phase 0 baseline audit for this milestone
  INSPIRATION.md    — Research sources consulted, concepts adopted/rejected, licenses
  THREAT_MODEL.md   — What is and is not protected, stated honestly
  release-process.md — Release checklist and version naming guide
  clean-room-and-voice-identity.md — What may and may not be taken from the
                       reference project, and what JARVIS's voice is and is not
  double-clap-activation.md — The one exception to the no-continuous-listening
                       rule: how narrow it is, how it was measured, what it cannot do
  dependency-review.md — Every dependency, its licence, and what this pass added
  mobile-companion-architecture.md — Considered and deliberately not built
  physical-pc-checklist.md — The 34 things no automated run can verify;
                       this release candidate is not finished until they pass
run_jarvis.py       — PyInstaller entry point (default: windowed launcher + tray;
                       --api: headless FastAPI server only; --cli: interactive REPL)
QUICKSTART.md       — First-run guide (included in release ZIP)
SECURITY.md         — Short security overview, points to docs/THREAT_MODEL.md
START_JARVIS.bat    — CLI launcher (included in release ZIP)
START_JARVIS_API.bat — API launcher (included in release ZIP)
SETUP_ENV.bat       — First-run .env setup helper (included in release ZIP)
```

---

## Security notes

- The API **only binds to 127.0.0.1** — it is never exposed to the network.
- The app launcher uses a **strict allowlist** — arbitrary executables cannot be launched.
- All tools pass through a **permission check** before execution; v0.2 adds a
  five-tier risk classification and a single policy engine
  (`app/core/policy.py`) on top of that, not instead of it.
- Blocked tool categories (password extraction, keylogging, etc.) are
  **not implemented** and are permanently refused by the permission system.
- No secrets are ever committed to version control.
- **v0.2 fix:** the dashboard's CORS `allow_origins` previously used glob
  patterns that were never actually enforced (see the v0.2 section above) —
  this is now a real, working allowlist.
- See **[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)** for the full,
  honest picture — including what is explicitly **not** protected (no OS
  sandboxing, no encrypted storage, no protection from a malicious local
  admin, and more). See **[`SECURITY.md`](SECURITY.md)** for the short
  version.

---

## Roadmap

| Phase | Goal | Status |
|---|---|---|
| 1 | Foundation: CLI, router, tools, API, SQLite | ✅ Done |
| 2 | Claude AI integration (natural-language fallback, conversation memory) | ✅ Done |
| 3 | TTS voice output (pyttsx3, local/offline, output-only — no microphone) | ✅ Done |
| 4 | Local browser dashboard (7 pages, dark UI, Jinja2 + vanilla JS) | ✅ Done |
| 5 | Action approval system (pending / confirm / cancel, Actions UI) | ✅ Done |
| 6 | Safe Windows actions expansion (URL opener, folders, notes, disk, network, battery) | ✅ Done |
| 7 | Professional UI/UX polish (sidebar layout, design system, metric cards) | ✅ Done |
| 8 | Screen intelligence (OCR, on-request only — explicit user permission required) | Planned |
| 9 | Browser automation (approval-gated, no autonomous browsing) | Planned |
| 10 | Smart home, health tracking, optional trading alerts | Planned |

> **v0.2** (infrastructure, not a numbered phase): Safe Voice Command
> Center and Windows Action Runtime — see the section near the top of this
> README, `docs/audit-v0.2.md`, and `docs/THREAT_MODEL.md`. Phase 8/9/10's
> planned scope above is unchanged by it.
