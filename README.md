# JARVIS — Personal Windows AI Assistant

> Phase 1: Foundation — modular, local-first, safe.

JARVIS is a local Windows AI assistant that brings together PC automation,
memory, system monitoring, and (in later phases) voice control and Claude AI —
all running privately on your machine, never in the cloud unless you choose to
enable it.

---

## What Phase 1 + Phase 2 includes

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
| Pytest test suite (80 tests) | ✅ |

## What is NOT included yet

- Voice control / wake word (Phase 3)
- Screen intelligence / OCR (Phase 4)
- Browser automation (Phase 5)
- Smart home / health / trading (Phase 6)
- Email sending
- AnyDesk / remote control
- Real-time dashboard UI
- Any network exposure (API is 127.0.0.1 only)

---

## Requirements

- Python 3.11+
- Windows 10/11 (for app launcher and screenshots; CLI/API work cross-platform)

---

## Installation

### Windows (recommended)

```bat
git clone https://github.com/dado211207/jarvis-windows-ai-assistant.git
cd jarvis-windows-ai-assistant
installer\JARVIS_SETUP.bat
```

Or with PowerShell:

```powershell
cd jarvis-windows-ai-assistant
.\installer\install.ps1
```

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

## Supported commands (Phase 1)

| Command | What it does |
|---|---|
| `help` | List all available tools |
| `status` | Show JARVIS version and config |
| `system status` | CPU, RAM, disk, battery |
| `open chrome` | Launch Chrome (Windows, allowlist only) |
| `open notepad` | Launch Notepad |
| `open calculator` | Launch Calculator |
| `open brave` / `open edge` | Launch Brave / Edge |
| `open vscode` | Launch VS Code |
| `open spotify` | Launch Spotify |
| `open discord` | Launch Discord |
| `screenshot` | Capture screen → `data/screenshots/` |
| `take screenshot` | Same as above |
| `memory add <text>` | Save a note to SQLite memory |
| `memory search <query>` | Search saved memories |
| `exit` | Quit JARVIS |

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
| `.env.example` | ✅ |
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
run_jarvis.py       — PyInstaller entry point (delegates to app.main)
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

| Phase | Goal |
|---|---|
| 1 ✅ | Foundation: CLI, router, tools, API, SQLite |
| 2 ✅ | Claude AI integration (natural-language fallback, conversation memory) |
| 3 | Voice input/output, wake word detection |
| 4 | Screen intelligence (OCR, visual context) |
| 5 | Browser automation (safe, approval-gated) |
| 6 | Smart home, health tracking, optional trading alerts |
