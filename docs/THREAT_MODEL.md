# JARVIS Threat Model (v0.2)

This document describes what JARVIS actually protects against, what it
does not, and why. It is written to be read alongside the code, not
instead of it — every claim below should be verifiable by reading the
referenced module. Where a protection is genuinely deferred rather than
built, that is stated plainly rather than implied.

## Scope

JARVIS is a **local-first, single-user desktop assistant**. It is designed
to run on one person's own Windows machine, under their own OS account,
serving only that person's own browser tabs on that same machine. It is
**not** designed to be exposed to a network, shared between users, or run
as a multi-tenant service.

## Trust model

**Trusted:**
- The person running JARVIS, on their own machine, under their own OS
  account.
- Application code registered through `ToolRegistry` (`app/core/tool_registry.py`).
  Only trusted, reviewed code can register a tool; nothing external can add one
  at runtime.

**Explicitly NOT trusted, by design:**
- Any AI provider's output (Anthropic API, or a future local model). Model
  output is free text only — see "The LLM never executes" below.
- Any web page that is not JARVIS's own dashboard, even if open in the same
  browser at the same time.
- The network, beyond the loopback interface.

**Not defended against at all** (see "Explicit non-goals" below):
- A malicious or compromised process running under the *same* OS account as
  the user, on the same machine.
- A malicious local administrator.

## Primary defenses, and where they live

| Defense | Where | What it actually does |
|---|---|---|
| Loopback-only binding | `app/config.py` (`jarvis_host` default `127.0.0.1`), `app/api/server.py:run_api` | The API is not reachable from the network unless the operator explicitly changes the host — CLAUDE.md requires explicit approval + security review for that change. |
| CORS origin allowlist | `app/api/origin.py`, wired into `app/api/server.py` | Only the dashboard's own `http://127.0.0.1:<port>` / `http://localhost:<port>` origins get CORS-permitted responses. **v0.2 fix:** the previous `allow_origins=["http://127.0.0.1:*", ...]` never matched anything — Starlette's `CORSMiddleware` matches by exact string, not glob — so this check was silently inert before this milestone. |
| WebSocket origin validation | `app/api/origin.py`, `app/api/ws.py` | Browsers do not apply CORS preflight to WebSocket handshakes, so this is enforced separately: a handshake with a *present but foreign* `Origin` header is rejected with close code 1008 before `accept()`. A *missing* Origin (non-browser tools connecting directly) is allowed, since a browser cannot forge a cross-origin WS handshake without sending a real, foreign Origin — see the docstring in `app/api/origin.py` for the full reasoning. |
| Deterministic routing; the LLM never executes anything | `app/core/router.py`, `app/core/policy.py` | Text commands are matched against a fixed, reviewed regex table (`ROUTES`) before any AI call is made. When a command *does* fall through to the AI provider (`brain.generate_response`), the result is plain text shown to the user — there is no code path from an AI response to a tool call. Every real tool invocation goes through `policy.evaluate()`, which is driven by each tool's registered `RiskLevel`, never by anything the model said. |
| Five-tier risk classification + policy engine | `app/core/models.py` (`RiskLevel`), `app/core/policy.py` | READ_ONLY / REVERSIBLE auto-execute; SENSITIVE always requires explicit approval; DESTRUCTIVE and BLOCKED are always denied outright in this milestone (no destructive tool is registered at all — see CLAUDE.md's blocked-capabilities list). One function (`evaluate()`) makes this decision; it is not re-derived ad hoc elsewhere (see `app/core/router.py::_dispatch`'s docstring for the earlier duplicate-decision issue this replaced). |
| Approval expiry + double-execution prevention | `app/core/pending_actions.py` (pre-existing, unchanged), `app/api/actions.py` | Pending approvals expire after 10 minutes. `PendingActionStore.confirm()`/`cancel()` are lock-protected; verified in this milestone under **real concurrent threads**, not just sequential double-calls — see `tests/test_concurrency.py`. |
| Persisted action audit trail | `app/core/action_lifecycle.py`, `db/migrations.py` (`action_lifecycle` table) | Every proposed action — auto-executed, approved, or denied — gets a durable record: tool, risk, policy decision and reason, timestamps, redacted input, result, duration. A record that reaches a terminal status can never be transitioned again (`TerminalStateError`), closing off a class of "resurrect a finished action" bugs. |
| Redaction before persistence/broadcast | `app/core/redaction.py` | Sensitive-looking keys (`password`, `token`, `secret`, `key`, `clipboard`, ...) are masked and long strings truncated before a tool's input is written to the audit trail or published as a WebSocket event. |
| Clipboard content never logged or broadcast | `app/desktop/clipboard.py` | `read_clipboard` is SENSITIVE and always requires approval. Its result *message* reports only a character count; the actual content lives solely in `data`, which reaches only the direct HTTP response to the specific `/actions/{id}/confirm` call the approving user made — never a log line, the audit trail's `result_summary`, or a WebSocket event. Verified directly in `tests/test_clipboard.py` and `tests/test_pipeline_integration.py`. |
| No shell execution | `app/desktop/apps.py`, `app/desktop/folders.py`, etc. | Every `subprocess` call uses an explicit argument list with `shell=False`, launching only an allowlisted executable path. Verified by `tests/test_safe_actions.py`. |
| Blocked URL schemes | `app/desktop/web.py` | Only `http://` and `https://` may be opened; `file:`, `javascript:`, `data:`, `powershell:`, `cmd:`, `vbscript:`, and others are rejected. |
| Safe path handling | `app/desktop/folders.py`, `app/desktop/notes.py` (pre-existing) | File operations are confined to specific allowed roots; see `tests/test_safe_actions.py`'s path-traversal tests. |
| Safe error envelope | `app/core/errors.py` | Exceptions from the Anthropic SDK and from tool handlers are classified **by exception type only, never by inspecting message text**, and returned to REST/WS/rendered output as a typed `SafeError` (category + fixed safe message + a correlation ID) — never the raw exception message, stack trace, or a local path. Full detail is still logged server-side under the same correlation ID for debugging. An earlier version of this document named the previous behavior (raw `str(exc)` reaching the browser) as an open gap; that gap is closed as of this defense. Verified in `tests/test_errors.py`, `tests/test_brain.py`, `tests/test_tool_registry.py`. |
| Bounded tool execution | `app/core/tool_registry.py` | Every tool call runs under a `ThreadPoolExecutor` with `future.result(timeout=...)`. A hang surfaces as a typed `TOOL_TIMEOUT` failure instead of blocking the request/approval pipeline indefinitely. Honest scope: Python has no safe way to force-kill a thread, so a hung handler's underlying thread is *not* killed — it is abandoned, and its eventual result is unconditionally discarded and can never mutate state after the timeout response is returned (`tests/test_tool_registry.py::test_orphaned_handler_result_is_never_observed_after_timeout`). |
| Per-session mutation token | `app/api/session.py` | State-changing REST endpoints and the WS handshake require a server-generated session token: a `SameSite=Strict`, non-HttpOnly cookie plus an `X-JARVIS-Session-Token` header the client must echo back, compared with `secrets.compare_digest`. An earlier version of this document named the absence of this token as an open gap — without it, a non-browser local process bypassing CORS entirely could call a mutation endpoint with only a forgeable Origin header. That gap is closed as of this defense. Never logged (verified via `caplog` in `tests/test_session_integration.py`). |
| Privacy mode | `app/core/privacy.py` | While on: new conversation turns are not persisted, `add_memory` rejects writes, `take_screenshot` refuses before ever calling `PIL.ImageGrab`. In-memory only, same reset-on-restart model as `pending_actions.py` — **this is not encryption and makes no such claim**; see "No encrypted storage" below, which still applies regardless of privacy mode's state. Verified in `tests/test_privacy.py` (19 tests) including direct proof the stored-memory path never reaches the Anthropic request payload. |
| Push-to-talk audio is never persisted | `app/api/routes.py` (`/voice/transcribe`), `app/voice/stt.py` | Uploaded audio is written to a temp file for the duration of transcription only and deleted in a `finally` block — success or failure — before the response is returned. No raw audio is written to the repo, the database, or a log line (logs record only the transcript's character count). Verified in `tests/test_voice_stt_endpoint.py`, including the failure path. |
| One provider boundary, no raw SDK errors | `app/core/ai/` | Providers are constructed with a `ProviderConfig` and never read global settings; they never raise a raw SDK exception past their own boundary, only a `ProviderError` carrying an `ErrorCategory`. The category — not the exception text — chooses the sentence the user reads, so a rate limit, an expired key, an unreachable local server and a timeout stay four distinguishable problems. Verified in `tests/test_ai_providers.py`, `tests/test_chat_pipeline.py`. |
| Streaming chat is not a second dispatch path | `app/api/chat.py` | `POST /chat/stream` asks `router.find_route()` first and executes a matched command through the ordinary policy-gated path. No tool is reachable through it that `POST /command` could not reach, and an approval-required command returns its pending action rather than streaming. Verified in `tests/test_chat_pipeline.py`. |
| Stop-generation is enforced at the boundary too | `app/api/chat.py`, `app/core/ai/base.py` | Providers check the cancellation token between chunks, *and* the streaming endpoint re-checks before yielding. "Nothing further appears on screen" therefore does not depend on every present and future provider implementing its half correctly — proven by a browser test driving a deliberately uncooperative provider. |
| Conversation history is privacy-gated | `app/core/conversation.py` | The last few turns are replayed so a follow-up question makes sense, bounded at `MAX_HISTORY_TURNS`. While privacy mode is on, stored turns are neither read nor written — reading them back out and sending them to a cloud provider would drive straight through privacy mode's own guarantee. Fails closed if the privacy module cannot be consulted. |
| Local AI is loopback-only, and never downloads | `app/core/providers.py`, `app/core/ai/ollama_provider.py` | Ollama is contacted at `127.0.0.1:11434` only; there is deliberately no setting for a remote host. A model the running instance does not report is refused by name rather than silently substituted, and `/api/pull` is never called from anywhere — enforced by a test that walks the AST of every module under `app/`. |
| Preferences are an allowlist, never a credential store | `app/core/preferences.py` | Only `STORABLE_KEYS` may be written to the JSON preferences file; anything else is refused rather than stored, so a browser-reachable settings write cannot become a general "set any config value" mechanism. API keys stay in the OS credential store. |
| Speech output is gated server-side | `app/api/routes.py` (`/voice/speak`), `app/voice/tts.py` | One flag (`tts_service.output_enabled`) decides whether JARVIS speaks, and the check lives on the server — a page left open before speech was switched off elsewhere cannot make it talk. Approval prompts are never read aloud. |
| Standing security invariants are tested, not reviewed | `tests/test_security_invariants.py` | Asserted against the assembled app and the source tree rather than a hand-maintained checklist: every mutating endpoint requires the session token (19 of 19 at time of writing), nothing binds `0.0.0.0`, no `shell=True`, no `eval`/`exec`/`__import__`, no pickle deserialisation, no credential literal, no `\|safe` in a template, every approval-required tool refuses `registry.execute()`, and no tool whose name matches this project's permanently-excluded capabilities is registered. |

## Explicit non-goals (do not assume these exist)

This list exists because it is easy to *imply* more security than is
actually built. None of the following are true of JARVIS today:

- **No full OS-level sandboxing.** Tool handlers run as normal Python code
  in the same process and OS user context as JARVIS itself. There is no
  container, no restricted token, no seccomp/AppContainer-equivalent
  isolation around a tool call.
- **No encrypted storage.** The SQLite database (`data/jarvis.db`) is
  plaintext on disk. Anyone with filesystem access to that file — or to
  the machine generally — can read memories, conversation history, and
  the action audit trail directly. There is no key management, no
  at-rest encryption, and no plan to silently claim otherwise; if this
  changes in a future milestone it will be documented here with what is
  actually implemented, not asserted in advance.
- **No safe arbitrary code execution.** JARVIS does not offer a sandboxed
  "run this code" capability at all — not safely, not unsafely. Arbitrary
  shell/PowerShell/Python execution is on the permanently-blocked list
  (CLAUDE.md) and there is no code path that reaches it.
- **No signed Windows installer.** The installer described elsewhere in
  this repository is unsigned. Windows SmartScreen and antivirus tools may
  (correctly) warn about it.
- **No protection from a malicious local administrator**, or from any
  other process already running with the same or higher privilege as the
  user's own OS account. If that account is compromised, JARVIS's own
  checks provide no additional boundary — the attacker already has
  everything JARVIS has.
- **No guarantee of AI provider correctness.** Claude (or any future
  provider) can be wrong, misleading, or simply unhelpful in its free-text
  responses. Nothing about JARVIS's architecture makes model output more
  *correct* — what the architecture guarantees is narrower and different:
  the model's output can never itself become a tool call. See "Deterministic
  routing" above.
- **Most tools' `verification_strategy` is descriptive, not enforced.**
  Declaring how a tool's effect *could* be verified (see each tool's
  `ToolDefinition` in `app/desktop/`) is not the same as JARVIS
  independently checking that the effect actually happened. For most
  tools in this milestone, "verification" is the tool's own reported
  success/failure — see the honest per-tool notes in each module.

## Threat scenarios considered

**A malicious web page open in another tab tries to control JARVIS.**
Blocked for WebSocket connections (Origin check, closes with 1008) and for
CORS-subject `fetch`/`XHR` calls (origin allowlist) in a standards-compliant
browser. Also blocked independently of Origin/CORS by the per-session
mutation token (see the defenses table above) — a non-browser local
process that bypasses CORS entirely still cannot call a mutation endpoint
without the session cookie and header, neither of which a foreign page or
script can read or forge.

**The AI provider returns a malicious or nonsensical instruction
("delete all my files").** The response is plain text. There is no
mechanism by which free text from `brain.generate_response` becomes a
tool call — only `app/core/router.py`'s fixed `ROUTES` table maps a
command to a tool, and that table is reviewed application code, not model
output.

**Two browser tabs (or a script) submit conflicting requests for the same
pending action at once.** `PendingActionStore.confirm()`/`cancel()` are
lock-protected; exactly one wins. Verified under real thread contention in
`tests/test_concurrency.py`, not merely by calling the method twice in a
row.

**A user approves `read_clipboard` while something sensitive is
copied.** The content is shown once, to that user, in that response. It
is never written to a log line, the audit trail, or a WebSocket event —
see the clipboard row in the defenses table above.

**Someone else with access to the same Windows account reads
`data/jarvis.db` directly.** Not defended against — see "No encrypted
storage" above. The Settings page states this rather than omitting it,
and shows what the file actually holds (counts only, never content) so
"your data stays local" is checkable rather than merely claimed.

**A local process tries to make JARVIS speak, or reads a stale page's
permission to do so.** The speech gate is server-side and reads one
flag. Switching speech off anywhere — the Voice page, a `speak off`
command, the CLI — takes effect for every open page immediately, because
none of them hold their own copy of the decision.

**A user asks the AI something while a previous answer is still
streaming, or navigates away mid-generation.** Generations are tracked
in an in-memory registry and cancelled cooperatively; the entry is
removed whether the stream finished, was stopped, or failed, so a
long-running process cannot accumulate abandoned generations. Stopping
is honest about its limit: it closes the upstream connection at the next
chunk boundary rather than claiming to abort an in-flight HTTP request.

**Someone asks JARVIS to forget everything, by accident or by a misheard
command.** `clear_memory` is approval-required and its preview names
what is *not* affected. Nothing is deleted before confirmation, and
cancelling deletes nothing — both verified against a real database.

**A user wants to know whether JARVIS phones home.** It does not. The
only outbound request the application makes is a chat message to the
provider the user configured; there is no telemetry, no analytics, no
crash reporting, and no update check — the "Check for updates" button
opens a page in the browser and says so. A test walks the AST of every
module to assert no update endpoint is contacted.

## Reporting a concern

This is a private, single-user personal project, not a public service.
If you (the owner) notice a gap this document doesn't already name
honestly, treat that itself as a bug: either the code should change, or
this document should be corrected to describe reality accurately. See
`SECURITY.md` for the short version of this file.
