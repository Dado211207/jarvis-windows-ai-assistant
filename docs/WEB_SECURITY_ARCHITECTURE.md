# Web Security Architecture — Design Constraints for a Future Web Version

**This document does not describe anything that exists in this codebase
today, and nothing in it is a request to build it.** JARVIS today is a
single-user, single-machine, localhost-only desktop application. This
document exists so that *if* a hosted/web version of JARVIS is ever built,
whoever builds it starts from an explicit, written list of what has to be
different — rather than rediscovering (or worse, not discovering) that the
desktop security model does not generalize to a network-facing one.

If you are reading this while implementing a web version: this document
sets the floor, not the plan. It says what must be true before anything
resembling the desktop app is reachable over a network from more than one
person's browser. It does not specify the implementation.

## The one rule everything else follows from

**The desktop app's localhost architecture must never be exposed directly
to the internet.** Not behind a reverse proxy, not with a port forward, not
"just for testing," not "just for me, from my phone." This is not a
configuration option to relax under pressure — it is a structural mismatch,
not a hardening gap that a firewall rule or an extra header closes. The
reasons are architectural, not just "insufficiently defended":

- **It assumes one user.** There is no concept of *which* user a request is
  from — anyone who reaches the process is the same single trusted
  identity. A second real user is not a smaller version of this problem;
  it's a different problem the current design has no answer for at all.
- **It assumes the machine's OS user boundary is the security boundary.**
  `app/api/local_guard.py`'s entire model — Host allowlist, exact-port
  Origin pinning, the per-launch session token — exists to stop a
  malicious *web page* from abusing a trusted *local* process. None of it
  stops a remote attacker who can simply reach the process directly, the
  way binding to `127.0.0.1` does today by construction.
- **It assumes the database is single-tenant.** `db/database.py`'s SQLite
  functions take no user/tenant identifier at all — `get_all_memories()`,
  `get_preferences()`, everything, returns *all* rows, because there has
  only ever been one person's rows to return. Exposing this to more than
  one user without redesigning the schema and every query is not a config
  change, it's a data leak by definition.
- **It assumes the API key belongs to whoever is running the process.**
  DPAPI-encrypted storage (see `docs/SECURITY.md`) ties the key to *one
  Windows user account, on one machine*. There is no concept of "whose
  request is this" to scope a key to — every request would use the same
  key, funded by whoever installed the app, regardless of who's asking.

Any pull request, script, deployment config, or ad-hoc instruction that
points `app.api.server` at `0.0.0.0`, a public port forward, a tunnel
(ngrok/Cloudflare Tunnel/similar), or any reverse proxy that makes it
reachable from outside the local machine is **prohibited**, full stop, on
this codebase as it exists today — see `CLAUDE.md`'s non-negotiable
"API stays local" rule, which this document does not soften. That
restriction lifts only when a genuinely separate web architecture, meeting
everything below, replaces the relevant pieces — not when someone adds
auth on top of the existing desktop API.

## What a real web version needs instead

None of this is optional, and none of it is "the desktop version plus a
login page." Each item below addresses a specific way the desktop
assumptions above break.

- **Proper user authentication.** A real identity system (password +
  modern hashing, or an established OAuth/OIDC provider) — not the
  per-launch session token, which authenticates "a browser tab talking to
  this one process," not a person.
- **Server-side sessions.** Session state lives on the server (or in a
  signed, server-verified token like a JWT with real expiry/rotation), not
  a value handed to the client to hold and return, and never derived from
  or compatible with the desktop app's session token scheme.
- **Per-resource authorization.** Every read and write checks *that this
  specific authenticated user* owns *this specific resource* — not just
  "is there a valid token," which is all the desktop model checks today.
- **Strict tenant/user isolation.** Every query is scoped by user/tenant
  ID, enforced at the query layer (and ideally the database layer too —
  see row-level security below), not assembled by convention and hoped to
  be correct everywhere it's called.
- **Server-side secret storage**, separate from the desktop's DPAPI
  approach: a proper secrets manager or an encrypted-at-rest store keyed
  per tenant, with access audited and rotated independently of any single
  user's session.
- **Database-level access controls** — e.g. Postgres row-level security or
  equivalent — as defense in depth *underneath* application-level
  authorization, not instead of it. If a query-layer bug ever forgets a
  `WHERE user_id = ?` clause, the database itself should still refuse to
  return another tenant's rows.
- **CSRF protection** appropriate to a real multi-user session model
  (e.g. the synchronizer-token pattern, or `SameSite` cookies plus origin
  checks) — not a repurposed version of the desktop's per-launch token,
  which was designed to prove "this request came from JARVIS's own page,"
  not "this request came from an authenticated session belonging to this
  specific user."
- **XSS protection**: the same discipline already practiced in the desktop
  UI (`textContent` only, never `innerHTML`, a strict CSP — see "Security
  response headers" in `docs/SECURITY.md`) carries over directly, but now
  defends session cookies/tokens that are actually worth stealing across
  users, not just a single-machine convenience token.
- **Strict CORS**, scoped to the web app's real, small set of production
  origins — never a wildcard, never a regex that matches attacker-chosen
  suffixes, the same anchored-exact-match discipline `local_guard.py`
  already uses for the desktop app's exact port, applied to real domains
  instead.
- **Rate limiting and abuse protection** on every endpoint, especially
  authentication and anything that spends the shared Anthropic API budget
  — the desktop app has never needed this (one user, their own key,
  their own machine); a shared service serving many users absolutely does.
- **Encrypted transport** — TLS, always, no exceptions, including for
  internal service-to-service calls if the deployment has more than one
  service.
- **Security logging and audit trails** — who did what, when, from where —
  sufficient to investigate an incident after the fact, distinct from
  JARVIS's current rotating debug log (which was never designed to answer
  "which user did this").
- **Account deletion and data export controls** — real, complete data
  lifecycle tools for each user's own data, not the desktop app's
  uninstall-time "delete everything under `%LOCALAPPDATA%`" — a web
  service holding multiple users' data needs per-user deletion/export that
  doesn't touch anyone else's.
- **Two-user isolation tests as a first-class, required test category** —
  not an afterthought. Any test suite for a web version must include tests
  that create two distinct users and assert, for every resource type, that
  user A can never read, list, modify, or delete anything belonging to
  user B — including through indirect paths (search, "recent items," IDs
  guessed or enumerated, error-message differences between "not found" and
  "not yours").

## Explicit prohibitions

These are not just "out of scope for now" — they are things a web version
must never do, because each one either recreates a single-user assumption
at multi-user scale or defeats a control this document just required:

- **No direct exposure of the local SQLite database** — not the file
  itself, not a query interface over it, not a debug endpoint that dumps a
  table. A web version needs a database designed for multi-tenant access
  control from the start, not the desktop app's single-user `jarvis.db`
  made reachable.
- **No reuse of the per-launch localhost session token as web
  authentication.** It was designed to answer "is this request really from
  JARVIS's own page, on this machine, this launch" — a question with no
  meaning once there's more than one machine, more than one launch, or
  more than one legitimate user. A web version needs real session/auth
  infrastructure, built for that purpose, from the start.
- **No API keys in the browser bundle**, ever, in any form — not the
  Anthropic key, not a proxy credential, not embedded in a source map. If
  the browser can read it, so can anyone who opens dev tools.
- **No unauthenticated public memory or diagnostics endpoints.** The
  desktop app's `/health` is deliberately the *only* endpoint reachable
  without a token, and even it reveals nothing but "the process is up" —
  see `docs/SECURITY.md`'s "Security response headers" and "Privacy and
  data minimization" sections. A web version's equivalent health check
  must be held to the same minimal-disclosure standard, and every other
  endpoint — especially anything touching memory, preferences,
  conversation history, or diagnostics — must require real per-user
  authentication and authorization, not "no Origin header means it's
  probably safe" reasoning that only ever made sense for a single local
  process.

## Scope of this document

This document is a design constraint list, not an implementation plan, a
timeline, or an approval to start work. No web version implementation
should begin as part of the pull request that introduced this document (or
any pull request whose primary purpose is desktop packaging/security
hardening); it is written down now so it exists *before* anyone is tempted
to shortcut it later under deadline pressure, not because a web version is
imminent.
