# JARVIS — Personal Windows AI Assistant

> Phase 8: Persistent Settings & Personality Memory. Windows installer and
> in-app first-run onboarding are in progress — see
> [`docs/WINDOWS_INSTALLER.md`](docs/WINDOWS_INSTALLER.md).

JARVIS is a local-first Windows AI assistant: PC automation, memory, system
monitoring, voice output, and optional Claude AI — all running privately on
your machine. Nothing is uploaded anywhere unless you add an Anthropic API
key, and even then only your typed messages are ever sent.

- **Using JARVIS?** See [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) — install,
  first run, everyday use, Settings, Diagnostics, uninstall.
- **Want the security model?** See [`docs/SECURITY.md`](docs/SECURITY.md) —
  where your API key is stored, what binds to what, what's logged.
- **Building or reviewing the installer?** See
  [`docs/WINDOWS_INSTALLER.md`](docs/WINDOWS_INSTALLER.md).
- **Testing the installer on real Windows?** See
  [`docs/WINDOWS_ACCEPTANCE_TEST.md`](docs/WINDOWS_ACCEPTANCE_TEST.md) —
  every item is unverified until someone actually runs it.
- **Contributing code?** Keep reading.

---

## What JARVIS does

| Area | Highlights |
|---|---|
| Chat / commands | Deterministic commands (system status, app launcher, screenshots, memory, notes) always work; open-ended questions fall back to Claude AI when a key is configured, otherwise a polite local message |
| Action approval | Anything higher-risk (e.g. clearing logs or memory) creates a pending action you must explicitly confirm or cancel — nothing risky runs silently |
| Voice output | Local/offline text-to-speech (pyttsx3) — output only, no microphone, no wake word, disabled by default |
| Memory & settings | Explicit-only personality memory and preferences, stored locally in SQLite; secrets are rejected before they're ever written |
| Dashboard | A local browser UI (Dashboard, Chat, Actions, Voice, Memory, Settings, Diagnostics, Help) bound to `127.0.0.1` only |

**Permanently excluded**, by design, not by omission: always-listening/wake
word, email sending without approval, remote control (AnyDesk-style),
network exposure beyond `127.0.0.1`, password/credential extraction,
keylogging or clipboard surveillance, network/port scanning, mass file
deletion. See `CLAUDE.md` for the full non-negotiable list this codebase is
built against.

**Planned, not yet built:** screen intelligence/OCR (on-request only),
approval-gated browser automation, smart home/health/trading integrations —
see the phase table in `CLAUDE.md`.

---

## Installing JARVIS (as a user)

See [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) for the full walkthrough.
Short version: download `JARVIS-Setup-<version>.exe` from the
[latest release](https://github.com/dado211207/jarvis-windows-ai-assistant/releases)
and run it — no Administrator rights needed. First-run setup (privacy
explanation, optional API key, voice preference) happens inside JARVIS
itself; nothing to edit by hand. (The installer is being built out on
`feat/windows-installer-onboarding` — see
[`docs/WINDOWS_INSTALLER.md`](docs/WINDOWS_INSTALLER.md) for current status
before that lands on `main`.)

---

## Contributing / running from source

Requirements: Python 3.11+. Windows 10/11 for the app launcher and
screenshot tools specifically; the CLI/API/tests all run cross-platform.

```bash
git clone https://github.com/dado211207/jarvis-windows-ai-assistant.git
cd jarvis-windows-ai-assistant
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # dev-only config; optional: add ANTHROPIC_API_KEY
```

Windows contributors can use the helper scripts instead of the manual steps
above — **these are developer environment scripts, not the end-user install
method**:

```bat
installer\DEV_SETUP_FROM_SOURCE.bat
```

or

```powershell
.\installer\dev_setup_from_source.ps1
```

### Running it

```bash
python -m app.main            # CLI REPL
python -m app.api.server      # Local API + browser dashboard at http://127.0.0.1:5555/ui/
```

Without `ANTHROPIC_API_KEY` set, JARVIS runs fully in local mode: every
deterministic command works normally, and open-ended questions get a polite
local fallback message instead of an AI response.

### Tests

```bash
python -m compileall app db   # compile check
pytest                        # full suite
```

CI (`.github/workflows/ci.yml`) runs the same on `ubuntu-latest` for every
PR and push to `main`. `.github/workflows/windows-build.yml` additionally
builds the PyInstaller executable and the Windows installer on
`windows-latest`, and smoke-tests the installer (silent install → health
check → silent uninstall) — see
[`docs/WINDOWS_INSTALLER.md`](docs/WINDOWS_INSTALLER.md).

---

## Architecture

```
app/
  main.py             — CLI entry point
  config.py           — Pydantic settings (dev: .env; production: AppData, see paths.py)
  logging_config.py    — Structured, rotating, bounded logging
  core/
    brain.py           — Orchestrator + Claude AI integration
    router.py          — Command → tool mapping (deterministic routes first)
    tool_registry.py   — Central tool registry + permission enforcement
    permissions.py     — SAFE / APPROVAL_REQUIRED / BLOCKED levels
    settings_service.py — Persistent user settings (allowlisted, secret-scanned)
    preferences.py     — Personality memory (explicit-only)
    secret_guard.py    — Rejects secrets before they reach settings/memory
    onboarding.py       — First-run setup state machine (installed app only)
    secret_store.py     — DPAPI-backed API key storage (installed app only)
    paths.py            — Single source of truth for every on-disk path
    launcher.py          — No-console production launcher (installed app only)
    migration.py         — Legacy ZIP-install DB migration (installed app only)
    diagnostics.py       — In-app support report
    update_check.py      — Metadata-only update check (see docs/SECURITY.md)
  desktop/            — Safe Windows actions: apps, screenshots, system, folders, notes, web
  api/                — FastAPI routes (command, actions, settings, preferences,
                         onboarding, diagnostics, update, voice)
  ui/                 — Jinja2 templates + vanilla JS dashboard (no innerHTML, no CDNs)
db/
  database.py          — SQLite access layer
  migrations.py         — Schema creation
installer/             — Inno Setup script + dev-only setup scripts (not the install method)
docs/                   — WINDOWS_INSTALLER.md, USER_GUIDE.md, SECURITY.md, release-process.md
tests/                  — Pytest suite
run_jarvis.py           — PyInstaller entry point (--cli / --api / production launcher)
```

### Design rules this codebase follows

See `CLAUDE.md` for the complete, authoritative list (it governs how Claude
Code sessions work on this repo too). The short version:

- **Modular, no giant files.** Each concern lives in its own module; tools
  register via `ToolRegistry`, never via `elif` chains.
- **Deterministic routing first.** The router's fixed routes are matched
  before anything falls through to the AI.
- **No autonomous tool execution by the AI.** Claude only ever responds
  with text; it cannot trigger tools or system actions on its own.
- **Approval-gated by default for anything risky.** Destructive or
  irreversible actions require explicit user confirmation.
- **Secrets never touch settings, memory, logs, or the browser.** The
  Anthropic API key lives in `.env` (dev) or DPAPI-encrypted storage
  (installed app) only.
- **Local-only.** The API binds to `127.0.0.1`; there is no cloud sync, no
  analytics, no tracking.

---

## Roadmap

| Phase | Goal | Status |
|---|---|---|
| 1 | Foundation: CLI, router, tools, API, SQLite | ✅ Done |
| 2 | Claude AI integration | ✅ Done |
| 3 | TTS voice output (local/offline, output-only) | ✅ Done |
| 4 | Local browser dashboard | ✅ Done |
| 5 | Action approval system | ✅ Done |
| 6 | Safe Windows actions expansion | ✅ Done |
| 7 | UI/UX polish (sidebar, design system) | ✅ Done |
| 8 | Persistent settings & personality memory | ✅ Done |
| — | Windows installer + in-app onboarding | In progress |
| 9 | Screen intelligence (OCR, on-request only) | Planned |
| 10 | Browser automation (approval-gated) | Planned |
| 11 | Smart home, health tracking, optional trading alerts | Planned |
