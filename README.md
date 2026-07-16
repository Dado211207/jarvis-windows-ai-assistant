# JARVIS — Personal Windows AI Assistant

> Phase 5: Action Approval System — risky actions require explicit confirmation before execution.

JARVIS is a local Windows AI assistant that brings together PC automation,
memory, system monitoring, voice output, and Claude AI —
all running privately on your machine, never in the cloud unless you choose to
enable it.

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
| Local browser dashboard (8 pages, dark UI) | ✅ |
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
| `clear memory` | Deletes all saved preferences / personality memory | High |

## Persistent settings & personality memory (Phase 8)

JARVIS now remembers safe preferences across restarts, stored locally in SQLite.

| Command | What it does |
|---|---|
| `settings` / `show settings` | Show your current settings |
| `set my name to <name>` | Tell JARVIS what to call you |
| `set assistant name to <name>` | Rename the assistant |
| `set language to <language>` | Set preferred language |
| `set response style to <short\|balanced\|detailed>` | Set preferred answer length |
| `set tone to <friendly\|formal\|casual\|neutral\|direct>` | Set preferred tone |
| `remember that <text>` | Explicitly save a preference |
| `what do you remember` | List saved preferences |
| `search memory <query>` | Search personality memory |
| `forget <text>` | Delete a single preference |
| `clear memory` | Delete all preferences (approval required) |

Memory is **explicit only** — JARVIS never saves anything silently or infers preferences
from ordinary chat. Secrets, passwords, API keys, tokens, cookies, and browser data are
**never** stored: any value that looks like a credential is rejected. Manage everything
visually on the **Settings** and **Memory** pages. `safety_mode` is always on and cannot
be disabled.

## What is NOT included (current alpha)

The following are **not** in the current release. Some are planned for later phases with explicit user confirmation and safety controls; others are permanently excluded.

**Planned later (with explicit user permission and safety controls):**
- Screen intelligence / OCR — on user request only
- Browser automation with approval gate
- Push-to-talk voice input — approval-gated, no always-listening
- Controlled computer-use actions with preview + confirmation

**Permanently excluded:**
- Always-listening / wake word (no microphone open in the background, ever)
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

---

## Installation

### Windows (recommended)

Download `JARVIS-Setup-<version>.exe` from the latest release and run it —
no Administrator rights required. See `docs/USER_GUIDE.md` for the full
first-run walkthrough. (This installer is being built out on the
`feat/windows-installer-onboarding` branch; see `docs/WINDOWS_INSTALLER.md`
for its current status.)

### Contributing / running from source

```bat
git clone https://github.com/dado211207/jarvis-windows-ai-assistant.git
cd jarvis-windows-ai-assistant
installer\DEV_SETUP_FROM_SOURCE.bat
```

Or with PowerShell:

```powershell
cd jarvis-windows-ai-assistant
.\installer\dev_setup_from_source.ps1
```

These are developer environment scripts, not the end-user install method.

### Manual / Linux / macOS (development)

```bash
git clone https://github.com/dado211207/jarvis-windows-ai-assistant.git
cd jarvis-windows-ai-assistant
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # optional: add ANTHROPIC_API_KEY
```

---

## First Run (Windows ZIP)

If you downloaded the release ZIP, use the included helper scripts — no
Python installation required.

| Script | What it does |
|---|---|
| `SETUP_ENV.bat` | Creates `.env` from `.env.example`; prints API key setup instructions |
| `START_JARVIS.bat` | Starts the JARVIS CLI assistant |
| `START_JARVIS_API.bat` | Starts the local FastAPI server on `127.0.0.1:5555` |
| `QUICKSTART.md` | Step-by-step guide for new users |

**Quick steps:**

1. Extract the ZIP (e.g. to `C:\JARVIS\`).
2. Double-click `SETUP_ENV.bat` — follow the on-screen instructions to
   add your Anthropic API key to `.env` (optional; required only for
   natural-language AI responses).
3. Double-click `START_JARVIS.bat`.

The API key is **never** included in the ZIP and is stored only in your
local `.env` file.

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
| `memory add <text>` | Save a note to local SQLite memory |
| `memory search <query>` | Search saved memories |

### Voice (TTS)

| Command | What it does |
|---|---|
| `speak on` | Enable TTS voice output for this session |
| `speak off` | Disable TTS voice output |
| `speak status` | Show TTS engine status |
| `speak test` | Speak a test phrase aloud |
| `stop speaking` | Stop current speech immediately |

### Approval-required

| Command | What it does |
|---|---|
| `clear logs` | Delete all action log entries — requires confirmation |

### API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Status JSON |
| GET | `/health` | Health check |
| POST | `/command` | `{"command": "system status"}` |
| GET | `/tools` | List all registered tools |
| GET | `/memory/search?q=...` | Search memory |
| GET | `/memory` | List recent memories |
| GET | `/logs` | Recent action logs |
| GET | `/voice/status` | TTS enabled / engine / available |
| POST | `/voice/speak` | `{"text": "hello"}` — speak text (requires TTS enabled) |
| POST | `/voice/stop` | Stop current speech |

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
| Tool registry | `tests/test_tool_registry.py` | Register, execute, error handling |
| Smoke | `tests/test_smoke.py` | Imports, routing, registry, API endpoints |
| Brain / AI | `tests/test_brain.py` | is_configured, AI mock, fallback, DB storage, API |

### CI pipeline

GitHub Actions runs on every **pull request** and every **push to `main`**:

- OS: `ubuntu-latest`
- Python: `3.11`
- Steps: checkout → install `requirements.txt` → `compileall` → `pytest`

Workflow file: `.github/workflows/ci.yml`

> Note: Windows-only tools (app launcher, screenshot) are tested at the
> routing/registration level only in CI. Actual OS calls are not made on Linux.

---

## Windows build artifact

A downloadable Windows build is produced automatically by GitHub Actions on every
pull request and push to `main`.

### How to download

1. Go to the [**Actions** tab](../../actions/workflows/windows-build.yml) in the
   repository.
2. Click the latest successful **Windows Build** run.
3. Scroll to **Artifacts** and download **JARVIS-Windows-Build.zip**.
4. Unzip it — you get a `JARVIS\` folder containing `JARVIS.exe`.

### What is included

| Item | Included |
|---|---|
| `JARVIS.exe` and its runtime libraries | ✅ |
| `README.md` | ✅ |
| `QUICKSTART.md` | ✅ |
| `.env.example` | ✅ |
| `START_JARVIS.bat` | ✅ |
| `START_JARVIS_API.bat` | ✅ |
| `SETUP_ENV.bat` | ✅ |
| Python runtime (embedded) | ✅ |
| All `app/` and `db/` modules | ✅ (compiled into the bundle) |

### What is NOT included

| Item | Excluded |
|---|---|
| `.env` / `ANTHROPIC_API_KEY` | ✅ Never included |
| `data/jarvis.db` (live database) | ✅ Created fresh on first run |
| `data/logs/` | ✅ Created fresh on first run |
| `data/screenshots/` | ✅ Created on demand |
| Test files | ✅ Not bundled |
| `.git` history | ✅ Not bundled |

### First-time setup on Windows

```bat
REM 1. Unzip JARVIS-Windows-Build.zip into a folder, e.g. C:\JARVIS\
REM 2. Copy .env.example to .env and add your API key (optional):
copy C:\JARVIS\JARVIS\.env.example C:\JARVIS\JARVIS\.env
REM    Then edit .env and add: ANTHROPIC_API_KEY=sk-ant-...
REM 3. Run JARVIS:
C:\JARVIS\JARVIS\JARVIS.exe
```

JARVIS runs entirely on your local machine. No data is sent to the network unless
you configure an `ANTHROPIC_API_KEY` for AI responses. The API key is **never**
included in the build artifact.

> **Note:** This is a development/testing artifact, not a signed Windows installer.
> Windows SmartScreen may warn on first launch — click **More info → Run anyway**.
> A signed installer is planned for a later phase.

Workflow file: `.github/workflows/windows-build.yml`

---

## GitHub Releases

Versioned Windows ZIPs are published to **GitHub Releases** via a separate
manual workflow.

### How to create a release

1. Go to **GitHub → Actions → Release** workflow.
2. Click **Run workflow**, enter a version tag (e.g. `v0.1.0`), click **Run**.
3. The workflow runs all tests, builds the executable, and publishes a release.
4. The release appears under **GitHub → Releases** with the ZIP attached.

### Release asset name

```
JARVIS-Windows-v0.1.0.zip
```

### What is and is not in the release ZIP

The release ZIP includes the same content as the Actions build artifact,
plus the first-run helper scripts and quick-start guide.
The `ANTHROPIC_API_KEY` and `.env` are **never bundled**.
Run `SETUP_ENV.bat` after extracting to create your local `.env`.

### Security and limitations

- The API key is **never** included in any release asset.
- FastAPI binds to `127.0.0.1` only.
- Releases are **unsigned** — Windows SmartScreen may warn on first launch.
  Click **More info → Run anyway**.
- A signed installer is planned for a later phase.

See [`docs/release-process.md`](docs/release-process.md) for the full release
checklist and version naming guide.

Workflow file: `.github/workflows/release.yml`

---

## Project structure

```
app/
  main.py           — CLI entry point
  config.py         — Pydantic settings
  logging_config.py — Structured logging setup
  core/
    brain.py        — Orchestrator + Claude AI integration
    router.py       — Command → tool mapping
    tool_registry.py — Central tool registry
    permissions.py  — Permission enforcement
    memory.py       — Memory tool wrappers
    models.py       — Shared Pydantic models
    system_prompt.py — JARVIS AI safety constraints
  desktop/
    apps.py         — App launcher (allowlist)
    screenshots.py  — Screenshot tool
    system.py       — System status (psutil)
    windows.py      — Windows utilities
  api/
    server.py       — FastAPI app
    routes.py       — Route handlers
db/
  database.py       — SQLite access layer
  migrations.py     — Schema creation
data/               — Runtime data (gitignored except .gitkeep)
tests/              — Pytest suite
installer/          — Windows setup scripts
docs/               — Developer process documentation
run_jarvis.py       — PyInstaller entry point (--api flag starts FastAPI server)
QUICKSTART.md       — First-run guide (included in release ZIP)
START_JARVIS.bat    — CLI launcher (included in release ZIP)
START_JARVIS_API.bat — API launcher (included in release ZIP)
SETUP_ENV.bat       — First-run .env setup helper (included in release ZIP)
```

---

## Security notes

- The API **only binds to 127.0.0.1** — it is never exposed to the network.
- The app launcher uses a **strict allowlist** — arbitrary executables cannot be launched.
- All tools pass through a **permission check** before execution.
- Blocked tool categories (password extraction, keylogging, etc.) are
  **not implemented** and are permanently refused by the permission system.
- No secrets are ever committed to version control.

---

## Roadmap

| Phase | Goal | Status |
|---|---|---|
| 1 | Foundation: CLI, router, tools, API, SQLite | ✅ Done |
| 2 | Claude AI integration (natural-language fallback, conversation memory) | ✅ Done |
| 3 | TTS voice output (pyttsx3, local/offline, output-only — no microphone) | ✅ Done |
| 4 | Local browser dashboard (8 pages, dark UI, Jinja2 + vanilla JS) | ✅ Done |
| 5 | Action approval system (pending / confirm / cancel, Actions UI) | ✅ Done |
| 6 | Safe Windows actions expansion (URL opener, folders, notes, disk, network, battery) | ✅ Done |
| 7 | Professional UI/UX polish (sidebar layout, design system, metric cards) | ✅ Done |
| 8 | Persistent settings & personality memory (local, explicit-only, no secrets) | ✅ Done |
| 9 | Screen intelligence (OCR, on-request only — explicit user permission required) | Planned |
| 10 | Browser automation (approval-gated, no autonomous browsing) | Planned |
| 11 | Smart home, health tracking, optional trading alerts | Planned |
