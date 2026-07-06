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

## Phase guide

| Phase | Status     | Scope |
|-------|------------|-------|
| 1     | ✅ Done     | Foundation: CLI, router, tool registry, permissions, SQLite, FastAPI |
| 2     | ✅ Done     | Claude AI integration, natural-language fallback via Anthropic SDK |
| 3     | ✅ Done     | TTS voice output (pyttsx3, local/offline, output-only, no microphone) |
| 4     | Planned    | Screen intelligence, OCR |
| 5     | Planned    | Browser automation |
| 6     | Planned    | Smart home, health, trading integrations |

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
