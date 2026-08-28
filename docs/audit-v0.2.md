# v0.2 Pre-Implementation Audit

Performed against `origin/main` at commit `20fbe1a` ("fix: repair YAML syntax
error in release.yml"), the actual tip of the default branch at the time this
milestone started. This document corrects the task's "known baseline" against
what is actually in the repository, per the instruction to verify every item
and correct the baseline where the code differs.

**Important context this audit surfaces:** the repository has two long-running
unmerged branches/PRs (a "Phase 8: Persistent Settings & Personality Memory"
branch and a Windows-installer/security-hardening branch stacked on it) that
were never merged into `main`. Neither is visible from `main` and neither is
part of this baseline. `main`'s own numbering treats "Phase 8" as future
work ("Screen intelligence / OCR"), which is a *different* thing than what
the unmerged branch called Phase 8. This v0.2 milestone is built on `main` as
it actually exists, not on either of those unmerged branches.

## Entry points

- `app/main.py` — CLI (`python -m app.main`), an interactive `jarvis>` REPL.
- `app/api/server.py` — FastAPI app factory (`create_app()`) + `run_api()`
  (`python -m app.api.server`, or `run_jarvis.py --api`).
- `run_jarvis.py` — PyInstaller entry point. `--api` starts the FastAPI
  server; no-flag starts the CLI. (No frozen/no-console production launcher
  exists on `main` — that was unmerged-branch-only work.)

## Package structure

```
app/
  main.py, config.py, logging_config.py
  core/    brain.py, router.py, tool_registry.py, permissions.py,
           pending_actions.py, memory.py, models.py, system_prompt.py
  desktop/ apps.py, folders.py, maintenance.py, notes.py, screenshots.py,
           system.py, web.py, windows.py
  api/     server.py, routes.py, actions.py
  ui/      routes.py, templates/*.html, static/{app.js,style.css}
  voice/   (does not exist as a package — TTS lives at app.voice.tts,
           imported directly; there is no app/voice/__init__.py-based
           package boundary beyond the module itself)
db/        database.py, migrations.py
tests/     12 files, 335 tests, all passing on this commit
installer/ JARVIS_SETUP.bat, install.ps1 (batch/PowerShell, no Inno Setup
           installer, no PyInstaller spec checked in)
docs/      release-process.md only (this audit and INSPIRATION.md are new)
```

No `app/ui/__init__.py`-based settings/onboarding/diagnostics modules exist.
No `db/migrations/` directory with versioned migration files — `db/migrations.py`
is a single idempotent `CREATE TABLE IF NOT EXISTS` script, run at `Brain.initialise()`.

## Current routes

**Root API** (`app/api/routes.py`, no prefix):
`GET /`, `GET /health`, `GET /system`, `GET /conversation`, `POST /command`,
`GET /tools`, `GET /memory/search`, `GET /memory`, `GET /logs`,
`GET /voice/status`, `POST /voice/speak`, `POST /voice/stop`.

**Actions API** (`app/api/actions.py`, prefix `/actions`):
`GET /actions/pending`, `GET /actions/{id}`, `POST /actions/{id}/confirm`,
`POST /actions/{id}/cancel`.

**UI** (`app/ui/routes.py`, prefix `/ui`): `/ui/` (+ `/ui/dashboard` alias),
`/ui/chat`, `/ui/logs`, `/ui/memory`, `/ui/voice`, `/ui/actions`, `/ui/help`.
**Seven pages**, not the eight the task's baseline and this repo's own README
both claim — `base.html` is a shared layout, not a page. Corrected in the
README update for this milestone.

Static assets are served from `/ui/static/` via `StaticFiles`, sourced from
`app/ui/static/` (2 files: `app.js` 691 lines, `style.css` 800 lines).

## Database tables

`memories`, `conversations`, `action_logs` only (`db/migrations.py`). No
`settings`, `preferences`, or any per-tool audit table. `action_logs` is a
flat, append-only table with no lifecycle states — a row is written once,
after the fact, with `status` in `('success', 'failure', 'blocked')`; there
is no "pending"/"executing" row, and no correlation to the in-memory
`PendingAction.id`.

## Command-routing flow

`CommandRouter.route()` (`app/core/router.py`): a linear list of ~28 regex
`Route` objects matched in order against the raw command string. First match
wins; on no match, falls through to `Brain.generate_response()` (Claude API
or local fallback) if a `Brain` is attached, else a generic "unknown command"
response. Dispatch (`_dispatch`) checks the matched tool's `PermissionLevel`
directly: `APPROVAL_REQUIRED` creates a `PendingAction` instead of executing;
anything else calls `registry.execute()` and logs the result. This is a
single, deterministic pipeline — there is no LLM involvement in routing or
tool selection for any recognized command, which already matches the
"deterministic intent routing" principle this milestone asks for; it simply
isn't named or exposed as an explicit state machine.

## Tool registry

`app/core/tool_registry.py`'s `ToolRegistry` is a plain `dict[str, RegisteredTool]`
keyed by tool name. `ToolDefinition` (`app/core/models.py`) has exactly four
fields: `name`, `description`, `permission_level`, `category`. No input/output
schema, no risk classification beyond the 3-value `PermissionLevel`, no
timeout, no reversibility flag, no verification strategy, no redaction rules,
no platform-support declaration. `execute()` and `execute_approved()` call
`tool.handler(**kwargs)` — handlers are plain Python callables registered by
each `app/desktop/*.py` module's `register_tools(registry)` function; kwargs
come directly from the router's regex capture groups, with no schema
validation layer before the call. This is the main gap the "typed tool
contract" work in this milestone addresses.

## Approval flow

`app/core/pending_actions.py`'s `PendingActionStore` is a thread-safe,
in-memory, lock-protected dict of `PendingAction` (Pydantic model) keyed by
UUID. States today: `pending → confirmed | cancelled | expired`, and
separately `executed | failed` set after execution. 10-minute expiry
(`EXPIRY_MINUTES = 10`), checked lazily on every `get`/`list_pending`/
`confirm`/`cancel` call rather than by a background sweep. `confirm()` and
`cancel()` both re-check `status == "pending"` *inside* the lock before
transitioning, which already prevents double-confirmation/double-execution
races at the store layer — a real, working safety property, not something
this milestone needs to invent from scratch. **Not persisted**: an app
restart loses all pending actions (documented, intentional). A browser page
refresh does **not** lose them, since the store lives server-side — the
existing behavior already satisfies "survive page refresh" as literally
requested; "survive restart" was never a requirement and remains
undocumented-as-such, which this audit is now making explicit.

Only one tool is currently `APPROVAL_REQUIRED`: `clear_logs`. Every other
registered tool — including `take_screenshot`, which this milestone's spec
places under "Sensitive and approval-required" — is `SAFE` today. See "Known
behavior this milestone intentionally does not change" below.

## Provider flow

`app/core/brain.py`'s `Brain.generate_response()` calls the Anthropic SDK
directly (`anthropic.Anthropic(...)`), inline, with no provider interface —
there is exactly one provider, hardcoded. Falls back to `_local_fallback()`
(a fixed string) on any exception or missing key; `error=str(exc)` is
returned to the caller **unredacted** — the raw SDK exception message
reaches `CommandResponse.data["error"]` and, from there, the browser. There
is no provider-relevance filtering of tools (moot today, since Claude is
never given tool-calling access at all — `system_prompt.py` confines it to
plain text, matching this milestone's "LLM must never directly execute OS
actions" requirement already, again without being named as such).

## Voice capabilities

`app/voice/tts.py` only. Text-to-speech via `pyttsx3`, offline, with a
session-enabled flag, rate/volume from settings, `is_available()`/`speak()`/
`stop()`. **No STT, no microphone access, no wake word, no push-to-talk** —
none of it exists in any form on `main`. `app/ui/templates/voice.html` is a
TTS-only control page. This is the largest gap between the task's
"known baseline" and the actual repository: the baseline lists "voice- and
text-accessible" and the CLAUDE.md Phase 3 rules on `main` are explicit and
current: *"Output only... No microphone input, no speech-to-text, no
always-listening behavior, no wake word — ever."* Any voice-input work in
this milestone must be introduced as new, clearly-optional capability, not
framed as restoring something that regressed.

## Security boundaries

- FastAPI binds to `127.0.0.1` (`app.config.settings.jarvis_host`, default,
  never overridden) — real and correctly enforced.
- **CORS is present but non-functional as configured**: `CORSMiddleware`
  is added with `allow_origins=["http://127.0.0.1:*", "http://localhost:*"]`.
  Starlette's `CORSMiddleware` matches `allow_origins` entries by *exact
  string equality* unless the single literal value `"*"` is used — a glob
  like `"http://127.0.0.1:*"` never matches a real origin such as
  `"http://127.0.0.1:5555"`. In practice this means the middleware never
  adds CORS headers for any real request, which happens to be harmless only
  because the dashboard is same-origin with the API it calls. This is a
  real, pre-existing bug, not a hardening gap this milestone introduces —
  flagged here and fixed as part of this milestone's security-hardening
  work, scoped narrowly to an equivalent-or-stricter exact-origin check.
- **No session/request token of any kind.** Every endpoint, including
  `POST /command`, `POST /voice/speak`, and `POST /actions/{id}/confirm`, is
  reachable by any request that can reach `127.0.0.1:5555` — there is no
  CSRF-equivalent protection today. This matters more once a WebSocket
  endpoint exists (an unauthenticated `ws://127.0.0.1:5555/...` is trivially
  reachable the same way), so this milestone adds a minimal, real per-session
  mutation token rather than deferring it.
- No rate limiting, no request-size limits, no CSP header, on `main` today.
- No secret ever reaches a Jinja2 template (verified: no `ANTHROPIC_API_KEY`
  or `sk-` string appears in any template context construction in
  `app/ui/routes.py`).
- `app.js` uses `textContent` exclusively — verified no `innerHTML` usage.

## Current tests and CI

335 tests across 12 files, **all passing** on this commit
(`python -m pytest -q` → `335 passed`). `python -m compileall app db` clean.
Breakdown by file: `test_approvals.py`, `test_brain.py`, `test_launcher.py`,
`test_permissions.py`, `test_router.py`, `test_safe_actions.py`,
`test_smoke.py`, `test_tool_registry.py`, `test_tts.py`, `test_ui.py`,
`test_ui_phase7.py`, plus `__init__.py`.

CI (`.github/workflows/ci.yml`): Ubuntu-only, `pytest` + `compileall`, no
Windows job runs tests today — `windows-build.yml` builds a PyInstaller
executable and does an artifact upload, but does not run `pytest` on
`windows-latest`. This milestone adds a real Windows-latest smoke job
without touching the existing Ubuntu job's behavior.

## Dead code, duplicate logic, unfinished placeholders

- **README.md's own banner is stale**: it says "Phase 5" in the title while
  its own roadmap table further down says Phases 1–6 are done, and
  `CLAUDE.md` (checked into the same commit) says 1–7 are done. Corrected as
  part of this milestone's README update.
- **Policy decision is not centralized**: `router.py::_dispatch` checks
  `PermissionLevel.APPROVAL_REQUIRED` directly to decide whether to create a
  pending action, while `permissions.py::check_permission` *also* encodes
  the same three-way decision (raises `ApprovalRequiredError`/
  `PermissionDeniedError`) — but `check_permission` is only actually called
  from inside `tool_registry.execute()`, which the router's approval branch
  never reaches (it intercepts before calling `execute()` at all). So there
  are two places that know about the SAFE/APPROVAL_REQUIRED/BLOCKED
  three-way split, and they can disagree in principle (nothing currently
  makes them disagree, but nothing enforces that they can't). This is
  exactly the "no duplicate policy decisions in routes and tools" problem
  this milestone's quality rules call out, and this milestone's policy
  engine consolidates it into one place.
- No dead files, no orphaned modules, no TODO/FIXME/placeholder stubs found
  in `app/` or `db/` (checked via grep for `TODO|FIXME|XXX|NotImplemented`
  across both trees — zero matches in application code).

## Windows-specific assumptions

- `app/desktop/apps.py`, `windows.py` assume Windows APIs
  (`os.startfile`, `subprocess` with Windows executable names) are present;
  guarded with `sys.platform` checks where it matters for CI safety, but
  actual app-launch/window behavior is untested on non-Windows (Linux CI
  only reaches the routing layer — confirmed by `test_smoke.py`'s explicit
  "routing only" framing).
- `app/desktop/screenshots.py` uses `PIL.ImageGrab`, which requires a
  display; Linux CI has none, so this is also routing-tested only.
- No Windows UI Automation, no `pywinauto`, no simulated input of any kind
  exists anywhere in the repository today.

## Existing behavior that must remain backward-compatible

- All 335 existing tests must keep passing unmodified in intent (their
  assertions about current, correct behavior are not touched); only tests
  whose expectations this milestone *deliberately and explicitly* changes
  (documented individually, not silently) may be edited, and none are
  deleted or weakened.
- `PendingActionStore`'s in-memory model, its 10-minute expiry, and its
  restart-resets-pending-actions behavior are preserved exactly — this
  milestone adds a persisted lifecycle *audit trail* alongside it, and does
  not replace or re-architect the store itself.
- `db/migrations.py`'s existing three tables and their data are never
  dropped, recreated, or migrated destructively — new tables are additive
  `CREATE TABLE IF NOT EXISTS` statements appended to the same idempotent
  script, exactly matching the project's existing migration style.
- `PermissionLevel` (SAFE/APPROVAL_REQUIRED/BLOCKED) keeps working exactly
  as today for every tool that exists on `main` right now; the new
  READ_ONLY/REVERSIBLE/SENSITIVE/DESTRUCTIVE/BLOCKED risk classification is
  introduced as additional, richer metadata used for new tools and for
  UI/policy display, layered on top rather than replacing the enforcement
  path existing tools already rely on. See the final report for the one
  place (screenshot's risk classification) where this milestone's own spec
  and `main`'s existing behavior disagree, and the explicit, non-silent
  decision made about it.
