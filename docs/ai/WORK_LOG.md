# Work log — Anthropic identity-linked API keys

A public, tracked handoff record. Not authorization, and not cross-session memory.
Code and current owner instructions take precedence if this becomes stale. No
secrets, personal data, absolute local paths, usernames or signed artifact URLs.

- Branch: `claude/anthropic-workspace-id`
- Base: `main` at `884c737d465afddeaee31ba99567345861602026` (verified live before
  any change)
- Purpose: JARVIS could not authenticate with an Anthropic identity-linked API
  key. Reproduced on the owner's real Windows 11 PC.
- **Nothing has been installed, merged, tagged, released or deployed.**

## Closed out: PR #18

PR #18 was squash-merged into `main` as `884c737` on 2026-08-31. It corrected a
WebView2 overclaim, a contradictory `EVENT_READY` comment, a blind spot in the
installer download guard, and added the pre-install WebView2 check
(`docs/physical-pc-checklist.md` section A0). Its post-merge runs on `884c737`
were green at attempt 1 — CI `33370652490`, Windows Build `33370652468`, Windows
Installer `33370652546` with job `99420801195` executing all 19 steps.

The real-PC acceptance that followed **passed** every step up to the AI provider:
WebView2 Runtime 151.0.4129.59 found, installer checksum matched, installation
completed, the native window rendered the real dashboard, database and health
healthy, and the Anthropic account itself proven working from the Playground.

The `msedge.exe` survivor remains unreproduced and unfixed; its opt-in
diagnostics scaffolding is deliberately untouched.

## The defect

A newly created Anthropic API key failed every request. Direct HTTPS calls made
**outside** JARVIS — `GET /v1/models` and `POST /v1/messages` — reproduced it
identically:

```
HTTP 400  invalid_request_error
anthropic-workspace-id is required when authenticating with an identity-linked
API key; send the id of the workspace this request acts in.
```

That rules out an invalid key, insufficient credit, a retired model, a network
fault, regional availability and Windows Credential Manager. It is a JARVIS
compatibility defect.

### Root cause, from Anthropic's own documentation

Anthropic's *Authentication* guide (`platform.claude.com/docs/en/manage-claude/authentication`)
splits API keys by what they act as:

| Key type | Acts as | Workspace header |
| --- | --- | --- |
| Personal key | the user | **required** unless the key was scoped to one workspace |
| Service account key | the service account | **required** unless scoped to one workspace |
| Workspace key (*legacy*) | the workspace itself | not needed |

Quoting the guide: *"API keys that are created for a specific workspace only work
in that workspace, and API requests using these keys can omit the workspace ID.
If your API key isn't scoped to a workspace, you must specify the workspace ID in
the `anthropic-workspace-id` header for each request."*

JARVIS accepted and stored **only** an API key. `ProviderConfig`,
`AnthropicProvider`, key verification, Setup and Settings had no concept of a
workspace, so an identity-linked key could not work at all.

Anthropic documents the value by prefix and by example —
`wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ` — and documents **no fixed length**, so
neither does this implementation.

### Two further defects the same session exposed

2. **The error was flattened.** The SDK raises `BadRequestError` for this and for
   a dozen unrelated malformed-request problems, so it fell through to the
   generic "The AI provider returned an error." A defect with a thirty-second fix
   read as an unexplained failure.
3. **The Logs page showed nothing**, so there was no second place to look.
4. **The dashboard claimed Claude was available** because a credential existed,
   while the provider was rejecting it on every request.

## The corrections

| # | Change | Where |
| --- | --- | --- |
| 1 | `anthropic_workspace_id` on the provider contract | `app/core/ai/base.py` |
| 2 | Shape, header and storage rules in one module | `app/core/ai/workspace.py` (new) |
| 3 | `default_headers` at the single client-construction point | `app/core/ai/anthropic_provider.py` |
| 4 | `PROVIDER_WORKSPACE_REQUIRED` + a narrow, documented marker match | `app/core/errors.py` |
| 5 | Key and workspace verified **atomically**, failing closed | `app/core/ai/key_check.py` |
| 6 | Optional workspace field, atomic storage, removal clears it | `app/api/routes.py` |
| 7 | Four-state credential model replacing "a key exists" | `app/core/providers.py` |
| 8 | A safe Logs row for every provider failure | `app/core/ai/events.py` (new) |
| 9 | Optional field + guidance on both pages | `setup.html`, `settings.html`, `app.js` |

**The header is added at `AnthropicProvider._client()`**, which is the only place
an Anthropic client is constructed and is shared by `generate()`, `stream()` and
key verification. Adding it on one path and not another would produce a key that
verifies and then fails in conversation, or the reverse. The mechanism is
`default_headers`, which is what Anthropic's own Python example uses ("Or set it
once for every request from this client") and which is present on
`anthropic.Anthropic.__init__` in the installed SDK (0.120.2, pinned `>=0.20.0`).

**A blank workspace ID sends no header at all** — not an empty one — so a legacy
workspace-scoped key makes byte-for-byte the request it made before.

**Storage.** The API key stays in Windows Credential Manager. The workspace ID is
account metadata, not a credential, and goes in `app/core/preferences.py` under
`anthropic_workspace_id`; the observed verification state goes beside it under
`anthropic_key_state`. Neither is ever returned by an endpoint, logged, or put in
a diagnostic — callers learn only *whether* a workspace is configured.

**Lifecycle.** Removing the Anthropic credential clears both, because they
describe that credential: leaving them would give the next key the workspace of
the one before it. A failed credential removal deliberately leaves them alone. An
ordinary uninstall keeps them, exactly as it keeps the credential;
`/DELETEDATA=yes` removes the data directory and therefore both. An upgrade from a
build that stored a key without a verification record reads as
`configured_unverified` — the honest answer, neither "verified" (which would
reinstate the defect) nor "failed" (which would libel a working key).

## Where to find the Workspace ID

In the Claude Console: **Settings → Workspaces**, the **ID** column. It starts
`wrkspc_`.

The Default Workspace is deliberately not listed there. Anthropic's guide gives
two ways to read it: from the `anthropic-workspace-id` **response header** of any
request that runs in it, or from `scope.workspace_id` on such a key in the Admin
API's List API Keys. A simpler route for most people is to create a key scoped to
a named workspace, which needs no header at all.

If the field is left blank and the key turns out to need one, JARVIS now says so
in one sentence and points at the Console, instead of "The AI provider returned an
error".

## The owner's current installation must not be patched manually

The installed build predates this fix. Do **not** hand-edit files under the
install directory, the AppData folder or the preferences JSON to add a workspace
ID: the installed binary has no code that reads it, so it would change nothing,
and a hand-edited preferences file is a state no test covers. The corrected
installer from this branch is the only supported route, and it is not yet
verified — see the verification gate in the pull request.

## Known gaps

- **Not verified against a real identity-linked key.** No test may use the
  owner's credentials, and none does. The header, the classification and the
  storage are proven against the documented contract and the verbatim error
  string; the end-to-end proof is a real-PC step the owner has to run.
- A workspace that exists but the key cannot access answers **404**, not 400.
  That is deliberately *not* pattern-matched — its message quotes the id back —
  and it already fails closed as `PROVIDER_ERROR`, so nothing is stored.
- `main` has no branch protection; no workflow declares `permissions:`; actions
  are pinned to mutable major tags; there is no SBOM. All public-release
  hardening, unchanged here.

## Next action

1. Independent review of this branch.
2. The verification gate in the pull request, including two sequential Windows
   Installer acceptance runs.
3. Only then, a real-PC upgrade-in-place by the owner, entering the Workspace ID
   in Settings.

Do not merge, tag, release, deploy, or install JARVIS without a current explicit
instruction.
