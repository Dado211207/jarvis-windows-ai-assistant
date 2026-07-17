# Security

This document covers where JARVIS stores things, what binds to what, and
what to expect from Windows around an unsigned build. For the installer's
own architecture, see [`WINDOWS_INSTALLER.md`](WINDOWS_INSTALLER.md). For
day-to-day usage, see [`USER_GUIDE.md`](USER_GUIDE.md).

## Local API binding

The FastAPI server binds to `127.0.0.1` only (`app/config.py`,
`jarvis_host`), never `0.0.0.0`. It is not reachable from any other device
on your network, and CORS/Origin/Host validation is enforced by
`app/api/local_guard.py` (see below). Changing the bind address requires an
explicit code change and a security review — see `CLAUDE.md`'s
non-negotiable rules, which this document does not override.

## Protection against a malicious website on the same machine

Binding to `127.0.0.1` stops other devices on the network, but it does not
by itself stop an unrelated website — open in the same browser, in another
tab — from directing the browser to send requests to JARVIS's local API,
nor does it say anything about which *paths* are safe to expose without
authentication at all. This is the general "localhost CSRF" problem, and it
matters here because this API can save/replace/delete your API key,
complete onboarding, change settings, add/delete memory, confirm or cancel
pending actions, run commands (which can launch allowlisted apps, take
screenshots, etc.) — and read back everything JARVIS knows: remembered
preferences, settings, conversation history, logs, diagnostics, pending
actions.

Every request goes through one policy point, `app/api/local_guard.py`'s
`LocalOnlyGuardMiddleware`. It applies three checks, in order:

1. **Host header allowlist**, on every request, always. Only `127.0.0.1`,
   `localhost`, or (test-only) `testserver` are accepted, and — whenever
   JARVIS has actually bound a real port (`runtime_state.set_actual_port()`,
   called by both real entry points before the server can receive its first
   request) — the port must match **exactly**. Anything else is rejected
   with `400`, no exceptions. A browser cannot forge its own Host header, so
   this specifically catches DNS-rebinding attempts (a public hostname that
   resolves to `127.0.0.1`) as well as a request aimed at some other local
   service that merely happens to share a hostname.
2. **Endpoint classification — default-private.** Every path requires the
   session token *unless* it is explicitly listed in `local_guard.is_public_path()`:
   the minimal `/health` status endpoint, `/docs`/`/redoc`/`/openapi.json`,
   static assets under `/ui/static/`, and the UI page shells themselves
   (`/ui/`, `/ui/chat`, `/ui/settings`, ...) — which carry no private data,
   only page structure plus the server-rendered token, and *are* the
   bootstrap mechanism a browser's first, tokenless request has to use.
   Everything else — every JSON endpoint that reads or writes application
   state, `GET` included — is private by default. A route that forgets to
   be added anywhere still gets classified as private automatically, so the
   failure mode of forgetting is "the new endpoint 401s until someone
   notices," never "silently public." `tests/test_private_endpoint_protection.py::test_no_private_endpoint_accidentally_public`
   walks the live FastAPI route table on every test run and fails if any
   `GET` route isn't covered by this classification, so this isn't something
   that has to be remembered by hand going forward.
3. **Origin allowlist**, applied to every request this classification marks
   private (reads and writes alike — not just state-changing methods). If
   an `Origin` header is present, it must be anchored, exact-string
   `http://127.0.0.1[:port]` or `http://localhost[:port]` — the same exact
   active port as the Host check, when known; anything else, including
   `null`, a lookalike like `http://127.0.0.1.attacker.example`, or a
   malformed port segment, is rejected with `403`. A *missing* Origin is not
   itself rejected here — see "Exact boundaries" below for why.
4. **Per-launch session token** (`app/core/session_token.py`), required as
   the `X-Jarvis-Token` header on every request this classification marks
   private, compared in constant time (`hmac.compare_digest`). This is the
   layer that actually stops the attack even when Origin passes or is
   absent: a `Content-Type` of `application/json` forces a CORS preflight
   (which the Origin allowlist above already fails for a foreign site), but
   a CORS-*safelisted* content type like `text/plain` does not trigger a
   preflight at all, and FastAPI does not itself reject a mismatched
   Content-Type before parsing the body — so a malicious page can get a
   "simple request" through Origin/CORS checks alone. Adding the custom
   `X-Jarvis-Token` header is what forces the preflight in that case too
   (custom headers are never CORS-safelisted), and the foreign page has no
   way to know the correct value to send even if it did get through:
   Same-Origin Policy stops it from reading JARVIS's own rendered HTML or
   `window` globals.

CORS preflight (`OPTIONS` with `Access-Control-Request-Method`) is handled
natively inside the same middleware rather than Starlette's `CORSMiddleware`
— that middleware's allowed-origin configuration is fixed once at
app-startup, before JARVIS has actually bound a port, so it structurally
cannot anchor `Access-Control-Allow-Origin` to the exact active port the way
`runtime_state.get_actual_port()` lets this middleware do at request time.
A preflight from an allowed origin gets back exactly the methods JARVIS
uses (`GET, POST, PUT, PATCH, DELETE` — no `TRACE`/`CONNECT`/etc.), exactly
the headers it needs (`Content-Type, X-Jarvis-Token`), a bounded cache
(`Access-Control-Max-Age: 600`), and never an
`Access-Control-Allow-Credentials` header — JARVIS uses no cookies, and
credentials are never paired with a reflected origin. `Access-Control-Allow-Origin: *`
is never used anywhere; the real origin is reflected back only when it
already passed the allowlist, with `Vary: Origin` alongside it.

Every response the token gate protects, and every UI page shell (since it
embeds the live token), also gets `Cache-Control: no-store, private` and
`Pragma: no-cache` — a browser disk cache, an intermediary proxy, or a
future launch must never be able to replay a stale, token-bearing response.

### Where the session token comes from, and where it can never go

The token is generated fresh (`secrets.token_urlsafe(32)` — 256 bits from
Python's `secrets` module, the same CSPRNG source used for password reset
tokens and API keys generally) every time the API starts — dev
(`python -m app.api.server` / `--api`) or the installed app's production
launcher, same `lifespan` code path in `app/api/server.py` — and lives only
in a module-level variable in memory for that process's lifetime:

- **Never persisted.** No file, no registry key, no environment variable,
  no SQLite table. A restart always gets a brand-new value; nothing about
  the previous run's token is recoverable afterward, by JARVIS or anyone
  else.
- **Never logged, never in diagnostics, never in an exception message.**
  Nothing that writes to the rotating log file ever includes it — the
  middleware's `logger.warning()` calls on a rejected request log the
  path/Origin/Host/method involved, never header *values* wholesale, and
  never the token specifically, correct or otherwise. `app/core/diagnostics.py`'s
  report (what the Diagnostics page displays and what "copy report" copies)
  is built as an explicit field allowlist — versions, booleans, paths, counts
  — with no code path that could reach a Python global like the token, let
  alone the page's own `window.__JARVIS_TOKEN__`. No screenshot tool or
  automation JARVIS ships reads page source or DOM state, only pixels.
- **Delivered only by being rendered into the page.** JARVIS's own HTML
  (`app/ui/routes.py`, `templates/base.html` / `onboarding.html`) embeds it
  server-side as `window.__JARVIS_TOKEN__`, inside the same trusted response
  the browser used to bootstrap the UI in the first place. It is never put
  in a URL (query string or otherwise, so it never reaches the address bar,
  browser history, or a referrer header), never in a cookie, never in
  localStorage/sessionStorage. A page from a different origin cannot read
  any of this due to Same-Origin Policy, so it has no way to obtain the
  token at all, correct or otherwise — and the response carrying it is
  marked `no-store` (above), so nothing persists it on disk either.
- **One deliberate exception for development**: running `--api` directly
  (an explicit, interactive terminal the caller already opened themselves)
  prints the token to that console so a developer can drive the API with
  curl or Swagger UI's "Try it out" — see `app/api/server.py::run_api`,
  which sets `app.state.print_token_on_startup = True` only on that one
  entry point. This is a console `print()`, never a `logger` call, so it
  never reaches the rotating log file; the production launcher
  (`app/core/launcher.py::_build_server`) never sets this flag, since its
  only legitimate consumer is the browser tab it opens itself, which gets
  the token the normal server-rendered way.
- **No generic bypass, in any environment.** There is no
  `X-Trusted-Client`-style header, no User-Agent-based trust, no
  "localhost means trusted," no "test mode means authorized." The one
  test-affecting environment variable in this codebase, `JARVIS_TEST_MODE`,
  only disables the production launcher's browser auto-open step for CI
  smoke tests (`app/core/launcher.py`) — it has no effect on
  `session_token.py` or `local_guard.py` whatsoever. The test suite obtains
  a real, valid token the same way any legitimate caller would: by asking
  the running app instance for the one it actually generated
  (`session_token.get_token()`, via `tests/conftest.py`'s `TestClient`
  patch) — there is no separate "test token" value anywhere, so a test
  proving a request is rejected is proving it against the exact same check
  a real attacker would face.

### Exact boundaries — what this does and does not protect against

This is CSRF/cross-origin protection plus endpoint classification, not a
general multi-user authentication system, and not a substitute for the
OS-level guarantees below it:

- It assumes the Windows user account itself is not compromised. Any
  process already running as you (malware, another app, a compromised
  browser extension with local file or process-memory access) can, in
  principle, still open the JARVIS window's actual DOM/JS context (e.g.
  via a debugger protocol) and read the token from there — this defends
  against a **web page**, not against **local code execution** as your
  user account, which is a different, larger threat this project does not
  claim to solve.
- Missing-Origin requests are not rejected outright, only checked against
  the token. This is deliberate, not an oversight: real browsers do send
  `Origin` on state-changing fetches (including same-origin ones) in every
  current major engine, so a request with **no** Origin header is far more
  likely to be a legitimate non-browser caller (curl, a developer's own
  script against `--api`, this project's test suite) than a
  browser-originated attack — and the token check still applies to it
  regardless, so "no Origin" is never sufficient by itself, only
  "no Origin *and* a valid token." (The CLI is not an example of this — see
  "The CLI never goes through HTTP at all" below, it doesn't send requests
  here in the first place.) If browser behavior around Origin ever changes,
  this assumption should be re-examined.
- The token is a shared-secret-per-process, not a per-user or per-request
  credential — anyone who can read it (i.e., anyone who can read JARVIS's
  own rendered page, which by construction means they're already running
  as you or have compromised your browser) can call any endpoint until the
  app restarts. It rotates on every restart but not continuously.
- None of this replaces the action-approval system (`app/core/router.py`,
  `PendingActionStore`) — that gate is about **what the user explicitly
  confirmed**, this one is about **who is allowed to ask at all**. Both
  apply independently.
- This is a single-user, single-machine trust model by design. It is not,
  and must never be treated as, a template for a multi-user or
  internet-facing deployment — see
  [`WEB_SECURITY_ARCHITECTURE.md`](WEB_SECURITY_ARCHITECTURE.md) for why a
  future web version needs a fundamentally different architecture, not an
  extension of this one.

### The CLI never goes through HTTP at all

`python -m app.main` (dev) and the frozen build's `--cli` flag / no-flag,
non-frozen fallback (`run_jarvis.py`) both resolve to the same
`app.main.main()` function, which calls `brain.process(raw)` **directly, in
the same process** — there is no `requests`/`httpx` call, no socket, no
loopback round-trip, and the module never imports anything under
`app.api.*`. The CLI's trust boundary is simply "it's the same OS process as
the router/brain/tool registry it's calling into," so there is nothing for
`local_guard.py` or `session_token.py` to gate — those only exist to protect
the *HTTP* surface, and the CLI never touches it.

This is deliberately not "the CLI gets a bypass token" or "the CLI is
exempt from the check" — there is no generic API-auth bypass anywhere in
this codebase, for the CLI or anything else. The CLI simply isn't an HTTP
client of its own API. `tests/test_api_security.py`'s
`test_cli_brain_process_bypasses_http_entirely` proves the CLI path works
correctly by calling `brain.process()` directly, with no `TestClient`, no
HTTP request, and no session token anywhere in the call — the same shape of
call the real CLI makes.

The two entry points that *do* start the real HTTP server are handled
differently, and neither needs a bypass either:

- `run_jarvis.py --api` (dev-only, always an explicit interactive terminal
  the caller already opened themselves) starts `app.api.server.run_api()`
  and prints the session token to that same console, so a developer can
  drive the API with curl or Swagger UI — see "One deliberate exception for
  development" above. The process printing the token and the process
  reading it back are the same human at the same terminal; there is no
  separate "CLI caller" identity involved.
- The frozen build's default path (`app.core.launcher.run_production()`)
  starts the server and opens the user's real default browser pointed at
  it — the launcher process itself never makes a request back to its own
  API. The browser tab it opens is just another browser client, and gets
  the token the same way any browser client does: server-rendered into the
  page it loads.

If a future change ever needs a *true* separate CLI-as-HTTP-client (for
example, a `--remote` flag pointed at an already-running instance), it
must obtain a real, valid, narrowly-scoped token through an explicit,
same-process or launcher-controlled mechanism — never a permanent token,
never a world-readable file, never an undocumented header — and that
mechanism must ship with its own tests before it ships at all. No such
mechanism exists today, and none should be added without updating this
document.

## Security response headers

`app/api/local_guard.py`'s `SECURITY_HEADERS` are applied to **every**
response the middleware touches — success, rejection (400/401/403), and
CORS preflight alike, HTML pages and JSON API responses alike — from one
place, so there's no per-route opt-in to forget:

| Header | Value | Purpose |
|---|---|---|
| `Content-Security-Policy` | `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'` | No remote scripts/styles/fonts/images/XHR targets of any kind; cannot be framed; no `<base>` retargeting; no plugins; forms only submit back to JARVIS itself |
| `X-Content-Type-Options` | `nosniff` | Browser must not guess a different content type than what JARVIS declares |
| `Referrer-Policy` | `no-referrer` | Following a link out of JARVIS (e.g. "API Docs") never leaks the referring URL |
| `X-Frame-Options` | `DENY` | Legacy fallback for engines that don't honor `frame-ancestors` |
| `Permissions-Policy` | `microphone=(), camera=(), geolocation=(), usb=(), payment=(), interest-cohort=()` | Denied outright, not merely restricted to `'self'` — JARVIS's TTS is output-only (Phase 3) and never requests any of these from the browser |

Two deliberate, narrow relaxations, both explained in code comments right
next to `SECURITY_HEADERS`:

- **`style-src 'unsafe-inline'`.** The templates use a couple dozen inline
  `style="..."` attributes for one-off layout tweaks. CSP has no
  nonce/hash mechanism for style *attributes* (only for `<style>`
  elements), so avoiding this would mean moving every one of them into a
  CSS class — inline styles cannot execute script by themselves in any
  modern browser, which is the actual thing CSP defends against.
  `script-src` carries no such relaxation: it is `'self'` only, full stop.
- **No inline `<script>` anywhere, by construction, not by CSP exception.**
  The one thing every page previously needed inline script for — handing
  the session token to the page's own JS — now happens via a `data-*`
  attribute on `<body>` (`templates/base.html`, `templates/onboarding.html`)
  instead of `<script>window.__JARVIS_TOKEN__ = "...";</script>`. So
  `script-src` needed no exception at all, rather than trading one down.

No `report-uri`/`report-to` directive is configured, so there is no channel
through which a CSP violation report — which can otherwise include page
URLs and blocked-resource details — is ever sent anywhere, local or remote.
`tests/test_api_security.py` proves the header set is present on success,
rejection, and preflight responses; that `frame-ancestors`/`X-Frame-Options`
both block framing; that `script-src` carries no `unsafe-inline`/`unsafe-eval`;
that no shipped template contains an inline `<script>` tag; and that none of
the shipped JS uses `eval(`, `new Function(`, or `document.write(`.

## Where your Anthropic API key is stored

| Mode | Storage | Plaintext? |
|---|---|---|
| Running from source (development) | `.env` in your working copy | Yes — this is a developer's own machine, outside JARVIS's threat model for the installed app, and `.env` is gitignored |
| Installed Windows app | `%LOCALAPPDATA%\JARVIS\config\secret.bin`, encrypted with Windows DPAPI (`app/core/secret_store.py`) | No |

The installed app never writes the key to `.env`, the SQLite database, a
log file, browser storage, or a crash report. The onboarding UI uses a
password-style input with an explicit show/hide toggle, and the key is
validated against Anthropic (a single lightweight API call) without ever
being logged. The API never returns the full key to the browser — only a
masked form (`sk-ant-…wxyz`) for display.

### Threat model for DPAPI storage

DPAPI (`CryptProtectData`/`CryptUnprotectData`) ties decryption to the
Windows user account that encrypted the data. This protects against:

- Casual inspection of the JARVIS install/data directory (another app, a
  backup tool, browsing the filesystem).
- Other Windows user accounts on the same machine.
- The key ending up in any of the plaintext locations listed above (all
  explicitly disallowed).

It does **not** protect against:

- Malware or another process already running as the same Windows user —
  DPAPI decrypts transparently in that context.
- An attacker with an unlocked, logged-in session as you.
- JARVIS's own process reading its own decrypted key (necessarily true of
  any local app that needs to use the key).

This is the same trust boundary most desktop apps that store a local API
key operate under, made explicit rather than left implicit. See
`app/core/secret_store.py`'s module docstring for the same detail in code.

## Where everything else lives

Centralized in `app/core/paths.py` — the single source of truth every other
module reads from:

| What | Development | Installed app |
|---|---|---|
| Database | `data/jarvis.db` | `%LOCALAPPDATA%\JARVIS\data\jarvis.db` |
| Logs | `data/logs/jarvis.log` | `%LOCALAPPDATA%\JARVIS\logs\jarvis.log` |
| Cache | `data/cache/` | `%LOCALAPPDATA%\JARVIS\cache\` |
| Backups (e.g. legacy-DB migration) | `data/backups/` | `%LOCALAPPDATA%\JARVIS\backups\` |
| Config (onboarding flag, secret storage) | `data/config/` | `%LOCALAPPDATA%\JARVIS\config\` |
| Program files | the repo checkout | `%LOCALAPPDATA%\Programs\JARVIS\` |

Nothing is ever installed under `C:\Program Files\` — that would require
Administrator rights, which normal JARVIS installation never needs.

## Logging

Logs are structured, rotating, and bounded (`app/logging_config.py`:
5 MB per file, 3 backups kept) — they cannot grow without limit. Log
messages never include the Anthropic API key, `.env` values, or full
request/response bodies that could contain a key.

## Privacy and data minimization

What the frontend receives is deliberately smaller than what the backend
itself knows. Two boundaries matter here, and they're enforced differently:

**What's private by default (Sections 2-4 above).** Every API response —
onboarding, Settings, Memory, Diagnostics, update-checks, `/health` —
carries only what that specific page needs, gated by the session token per
the endpoint classification in `app/api/local_guard.py`. `/health` in
particular never carries database status, provider/API-key status, or
anything else beyond "the process is up" (see `HealthResponse` in
`app/api/routes.py`); everything richer lives on the token-protected `/`
and `/diagnostics` endpoints instead. Nothing here ever returns the
Anthropic API key itself (masked at most — `secret_store.mask_api_key()`),
an encrypted key blob, DPAPI-internal metadata, the session token, or a
complete `Authorization` header.

**What's redacted because JARVIS doesn't fully control its shape
(`app/core/redact.py`).** Some text JARVIS shows the user or hands back
over the API originates from an OS/SDK exception, not from JARVIS's own
code — a failed tool call, a failed AI-provider request, a failed
migration, a failed "open logs folder." Unlike everything else in this
app's user-facing text, that text cannot be trusted by construction to be
free of a Windows username (which turns up in nearly every real Windows
path — `C:\Users\<name>\...`), an accidentally-embedded key/token, or an
email address. `redact_text()` is applied at every point one of these
crosses into a user-facing response:

- `app/core/tool_registry.py`'s `execute()`/`execute_approved()` — a tool
  handler's exception message, which becomes the chat message the user
  sees for a failed command.
- `app/core/brain.py`'s `generate_response()` — a failed Anthropic API
  call's exception, which reaches the frontend via
  `CommandResponse.data["error"]`.
- `app/core/diagnostics.py`'s `open_logs_folder()` — a failed
  file-manager launch.
- `app/core/diagnostics.py`'s `get_report_text()` — the Diagnostics page's
  "Copy report" button. This is the one place the distinction between
  "safe to *display*" and "safe to *copy elsewhere*" matters explicitly:
  `get_report()` (backing the page's own on-screen fields) still shows the
  real database/log paths, since that's the user's own machine and a real
  path is useful for their own troubleshooting; `get_report_text()` (what
  "Copy report" actually copies, served from the separate, token-protected
  `GET /diagnostics/report-text`) redacts the same fields plus the legacy-
  migration marker's `source`/`error`, which can carry the same kind of
  path — because that text is meant to be pasted into a bug report, a
  chat, somewhere JARVIS has no visibility into and no control over who
  reads it next.

Paths where an exception is already curated instead of raw needed no
change: `app/core/onboarding.py`'s API-key validation classifies every
`anthropic.*` exception into one of a handful of fixed, generic messages
(`_classify_error`) rather than ever using `str(exc)`, and
`app/core/secret_store.py`'s `SecretStoreError` messages are either fixed
strings or `type(exc).__name__` only (the exception's class name, never
its message). `app/core/update_check.py` follows the same pattern for
network failures. FastAPI itself runs without `debug=True`, so an
unhandled exception anywhere else returns a generic 500 with no traceback,
never framework-level exception detail.

`tests/test_diagnostics.py` adversarially exercises `redact_text()` with
API-key-like strings, bearer tokens, `Authorization` header values (both
plain and JSON/dict-repr style), email addresses, Windows *and* Unix
home-directory paths, a multiline exception/traceback with a path buried
partway through, and an HTML/JS-like value that must pass through
unmangled (not a targeted category, but must not crash or corrupt). The
same fixtures are re-used at each of the four call sites above to confirm
the redaction actually reaches the response, not just the shared helper in
isolation.

## Uninstall data handling

Uninstalling JARVIS removes the installed program files only. Your data
(`%LOCALAPPDATA%\JARVIS\`) is preserved by default; deleting it requires an
explicit "Yes" to a prompt whose default answer is "No" — a silent or
scripted uninstall always takes the "preserve data" path. See
`installer/JARVIS.iss`.

## Code signing status

**This build is currently unsigned**, and nothing in this project fakes or
claims a signature it doesn't have. On first run, Windows SmartScreen will
likely show an "unrecognized app" warning — this is standard for any new,
unsigned publisher and is not a bug. Click **More info → Run anyway**.

The installer script and CI build are structured so a real Authenticode
signing step (a `SignTool=` directive backed by a certificate supplied via
CI secrets) can be added later without restructuring anything — but no
signing is enabled today, and none is faked in the meantime.

## Update checking

JARVIS can check GitHub for a newer release (Settings → "Check for
updates"), but only compares version metadata — it never downloads or
executes anything automatically; at most it links to the release page for
you to download yourself. As of this writing, the source repository is
private, so the public (unauthenticated) Releases API cannot be queried
without exposing a credential — a credential this project will not ship to
every installed user's machine. Update checking is therefore disabled
outright (not attempted, not silently failing) until the repository is
public. See `app/core/update_check.py` for the exact gate and how it's
verified.

## No autonomous or surveillance capability

Per `CLAUDE.md`'s non-negotiable rules, this codebase does not and will
not implement: always-listening/wake-word audio capture, keyloggers,
clipboard sniffers, webcam capture, continuous screen recording, password
extraction (browser/OS/WiFi), network or port scanning, or autonomous tool
execution by the AI (Claude only ever responds with text; it cannot invoke
tools on its own). Anything that deletes files, changes system settings, or
sends data externally requires your explicit confirmation through the
action-approval system first.
