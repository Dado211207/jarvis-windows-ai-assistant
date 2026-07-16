# Security

This document covers where JARVIS stores things, what binds to what, and
what to expect from Windows around an unsigned build. For the installer's
own architecture, see [`WINDOWS_INSTALLER.md`](WINDOWS_INSTALLER.md). For
day-to-day usage, see [`USER_GUIDE.md`](USER_GUIDE.md).

## Local API binding

The FastAPI server binds to `127.0.0.1` only (`app/config.py`,
`jarvis_host`), never `0.0.0.0`. It is not reachable from any other device
on your network, and CORS is restricted to `127.0.0.1`/`localhost` origins
(`app/api/server.py`). Changing the bind address requires an explicit code
change and a security review — see `CLAUDE.md`'s non-negotiable rules,
which this document does not override.

## Protection against a malicious website on the same machine

Binding to `127.0.0.1` stops other devices on the network, but it does not
by itself stop an unrelated website — open in the same browser, in another
tab — from directing the browser to send requests to JARVIS's local API.
This is the general "localhost CSRF" problem, and it matters here because
this API can save/replace/delete your API key, complete onboarding, change
settings, add/delete memory, confirm or cancel pending actions, and run
commands (which can launch allowlisted apps, take screenshots, etc.).

Three layers, applied to every request (`app/api/local_guard.py`):

1. **Host header allowlist**, on every request. Only `127.0.0.1`,
   `localhost`, or (test-only) `testserver` are accepted; anything else is
   rejected with `400`. A browser cannot forge its own Host header, so this
   specifically catches DNS-rebinding attempts (a public hostname that
   resolves to `127.0.0.1`).
2. **Origin allowlist**, on state-changing requests only (`POST`/`PUT`/
   `PATCH`/`DELETE`). If an `Origin` header is present, it must be
   `http://127.0.0.1[:port]` or `http://localhost[:port]`; anything else,
   including `null`, is rejected with `403`. A *missing* Origin is not
   itself rejected here — see "Exact boundaries" below for why.
3. **Per-launch session token** (`app/core/session_token.py`), required as
   the `X-Jarvis-Token` header on every state-changing request, compared in
   constant time (`hmac.compare_digest`). This is the layer that actually
   stops the attack: a `Content-Type` of `application/json` forces a CORS
   preflight (which the Origin allowlist above already fails for a foreign
   site), but a CORS-*safelisted* content type like `text/plain` does not
   trigger a preflight at all, and FastAPI does not itself reject a
   mismatched Content-Type before parsing the body — so a malicious page
   can get a "simple request" through Origin/CORS checks alone. Adding the
   custom `X-Jarvis-Token` header is what forces the preflight in that case
   too (custom headers are never CORS-safelisted), and the foreign page has
   no way to know the correct value to send even if it did get through:
   Same-Origin Policy stops it from reading JARVIS's own rendered HTML or
   `window` globals.

The token is generated fresh (`secrets.token_urlsafe(32)`) every time the
API starts — dev (`python -m app.api.server` / `--api`) or the installed
app's production launcher, same code path — and lives only in memory for
that process's lifetime:

- **Never persisted.** No file, no registry key, no environment variable.
  A restart always gets a brand-new value.
- **Never logged.** Nothing that writes to the rotating log file ever
  includes it.
- **Delivered only by being rendered into the page.** JARVIS's own HTML
  (`app/ui/routes.py`, `templates/base.html` / `onboarding.html`) embeds it
  server-side as `window.__JARVIS_TOKEN__`. It is never put in a URL
  (query string or otherwise), never in a cookie, never in
  localStorage/sessionStorage — a page from a different origin cannot read
  any of this due to Same-Origin Policy, so it has no way to obtain the
  token at all, correct or otherwise.
- **One deliberate exception for development**: running `--api` directly
  (an explicit, interactive terminal the caller already opened themselves)
  prints the token to that console so a developer can drive the API with
  curl or Swagger UI's "Try it out". This is a console print, not a log
  write, and only happens for that one explicit entry point — the
  production launcher never does this, since its only legitimate consumer
  is the browser tab it opens itself.

CORS itself was also tightened as part of this: the previous
`allow_origins=["http://127.0.0.1:*", "http://localhost:*"]` used a literal
`*` character in a plain string, which Starlette's CORS middleware matches
exactly, not as a glob — it never actually matched any real `Origin` header
at all. It's now `allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$"`,
which correctly matches any port. `Access-Control-Allow-Origin: *` is never
used — the middleware always reflects the specific matched origin, or
omits the header entirely for anything that doesn't match.

### Exact boundaries — what this does and does not protect against

This is CSRF/cross-origin protection, not a general authentication system,
and not a substitute for the OS-level guarantees below it:

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
  current major engine, so a state-changing request with **no** Origin
  header is far more likely to be a legitimate non-browser caller (curl, a
  developer's own script against `--api`, this project's test suite) than
  a browser-originated attack — and the token check still applies to it
  regardless. If browser behavior around Origin ever changes, this
  assumption should be re-examined.
- The token is a shared-secret-per-process, not a per-user or per-request
  credential — anyone who can read it (i.e., anyone who can read JARVIS's
  own rendered page, which by construction means they're already running
  as you or have compromised your browser) can call any state-changing
  endpoint until the app restarts. It rotates on every restart but not
  continuously.
- This layer does not add authentication to `GET` requests (health checks,
  reading settings/memory/diagnostics). Those remain unauthenticated by
  design — read-only local information disclosure to anything already
  running as your Windows user is accepted here, consistent with how a
  typical localhost dev server behaves; only *state changes* are gated.
- None of this replaces the action-approval system (`app/core/router.py`,
  `PendingActionStore`) — that gate is about **what the user explicitly
  confirmed**, this one is about **who is allowed to ask at all**. Both
  apply independently.

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
request/response bodies that could contain a key. The Diagnostics page's
"Copy sanitized report" button assembles a report from booleans, counts,
and paths only — never secret material (see `app/core/diagnostics.py`).

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
