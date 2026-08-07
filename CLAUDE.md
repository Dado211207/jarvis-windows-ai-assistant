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

## Phase 3 TTS rules (non-negotiable)

- **Output only.** Phase 3 TTS is text-to-speech output. No microphone input,
  no speech-to-text, no always-listening behavior, no wake word — ever.
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

## Do NOT implement in this repo (ever, without explicit separate design review)

- Password extraction (browser, OS, WiFi)
- Remote control / AnyDesk automation
- Email sending without approval flow
- Mass file deletion
- Network scanning or port scanning
- Anything that could be used for surveillance

## Testing

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
