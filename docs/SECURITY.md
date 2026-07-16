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
