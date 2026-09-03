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
the one before it. A failed credential removal deliberately leaves them alone,
and a removal that clears the credential but *cannot* clear the metadata is
reported as exactly that rather than as a clean removal. An ordinary uninstall
keeps them, exactly as it keeps the credential; `/DELETEDATA=yes` removes the
data directory and therefore both. An upgrade from a build that stored a key
without a verification record reads as `configured_unverified` — the honest
answer, neither "verified" (which would reinstate the defect) nor "failed" (which
would libel a working key).

## Where to find the Workspace ID

**The route that needs no ID at all, and the one to recommend.** Anthropic:
"You can also scope the key to a specific workspace, which lets you skip setting
a workspace ID manually in future requests." The Default Workspace can be that
workspace like any other — the FAQ notes that for a key belonging to it, the
key's `scope` field "carries its real ID" — so this works on an account that has
never created a second workspace. **Nobody has to create an extra workspace to
use JARVIS.**

**A non-default workspace:** Claude Console, **Settings → Workspaces**, the
**ID** column. It starts `wrkspc_`.

**The Default Workspace is not in that table**, and this is the correction the
first draft of this branch needed. Anthropic states it twice:

> Default Workspace has a `wrkspc_` ID like any other workspace (returned in the
> `anthropic-workspace-id` response header … and accepted by Get Workspace), but
> it doesn't appear in List Workspaces results

> List Workspaces omits the Default Workspace; its ID is in the
> `anthropic-workspace-id` response header of any request that runs there.

The response header is therefore the documented, reproducible route, and
`docs/WINDOWS_INSTALLER.md` carries the exact PowerShell command. (`scope` on
List API Keys is a second documented route but needs an Admin API key, so it is
not offered to end users.) The header is absent exactly when it would be most
wanted — a multi-workspace key with no header fails before resolving to a
workspace — which is why the scoped-key route is the recommended one rather than
a footnote.

`tests/test_workspace_guidance.py` fails if any surface names the table without
the exception, claims List Workspaces returns Default, drops the scoped-key
route, or tells the owner to create a workspace.

If the field is left blank and the key turns out to need one, JARVIS says so in
one sentence instead of "The AI provider returned an error".

## Independent review, second pass — six blockers

The `anthropic-workspace-id` implementation was found directionally correct, and
six defects were found around it. All are fixed on this branch; each has
regression tests written against the failure first.

1. **Default Workspace guidance was not actionable** — the section above.
2. **The two stores were not one operation.** The key goes to Credential Manager
   and the workspace ID to `preferences.json`; the second write's result was
   discarded, so replacing a key could leave the new credential beside the
   *previous* key's workspace and verdict and still answer "saved and verified".
   `app/core/ai/credential_pair.py` now snapshots, writes, and compensates:
   rollback on a failed metadata write, a metadata clear if the rollback itself
   fails, and a precise partial-failure report if that fails too. It deliberately
   does **not** claim atomicity — there is no transaction spanning a credential
   store and a JSON file — it claims that every failure ordering ends in a state
   that is either correct or described. An unreadable snapshot never authorises a
   destructive rollback, so a failed replacement cannot delete a working key.
3. **The path that exposed the original defect wrote no Logs row.**
   `verify_anthropic_key()` now records one safe `ai_provider` event per failure,
   with a reference id and nothing else.
4. **`to_safe_error()` logged the raw exception.** Anthropic's documented 404 is
   ``Workspace `<id>` not found.``, so `exc_info=exc` wrote a workspace ID into
   `jarvis.log`. `app/core/safe_traceback.py` describes an exception — type
   chain, traceback frames, trimmed paths — and never renders its value. The same
   pass removed `exc_info=True` from `credentials._mutate()`, where a keyring
   backend exception may quote the key it was asked to store; that file's own
   `_run_isolated()` had refused `str(exc)` for exactly this reason since it was
   written.
5. **A check that could not run was recorded as a rejection.** `verified if ok
   else failed` meant an offline save produced "The saved API key was rejected by
   Anthropic". `providers.state_for_verification()` now distinguishes answered /
   could-not-complete / unfunded, `note_runtime_failure()` downgrades a stored
   "verified" when a *live* request is explicitly rejected, and "verified" is
   worded as "answered successfully the last time Anthropic was asked".
6. **First-run copy still described a two-field screen** and claimed every key is
   checked before saving, which is false by design for a key that could not be
   checked at all.

All installer evidence from the previous head is **superseded**: this commit
changes runtime, Setup/Settings, credential persistence and packaged behaviour.
The workflow run IDs, job IDs and the artifact's size and SHA-256 for the new
head live in the pull request rather than here — writing a digest into a tracked
file changes the commit it describes, so the file could never hold its own
artifact's hash.

## Independent review, third pass — four blockers in the correction itself

The corrective commit was reviewed again and four defects were found **in it**.
Each has regression tests written against the failure first: 27 of the 41 new
tests in `tests/test_credential_replacement_safety.py` failed on the previous
head, and the two that matter most failed with `a failed replacement destroyed
the key it was replacing`.

### 1. A failed replacement could delete the key it was replacing

`credentials._mutate()` reconciled *every* failed non-None write to absence:

```python
if value is not None:
    cleanup_generation = _record_desired(username, None)   # queue a delete
```

That is correct for a first-time save — a late `set_password` would otherwise
leave a credential the user was told had not been saved. It is destructive for a
replacement, because the old key and the new key are the same Credential Manager
entry. Save B over A, have the write fail or time out, and the reconciler
deletes A while `credential_pair.save()` answers "Nothing was changed."

The reconciliation target is now the value that was **proven** to be there
beforehand. Establishing it is a precondition of attempting the write: a store
JARVIS cannot read is a store it will not write to, because a failure there
could not be undone. An unreadable entry is never treated as an empty one.
`credential_pair.save()` refuses for the same reason rather than starting a
replacement whose rollback it knows it cannot perform.

### 2. Removal claimed postconditions it had not established

A delete that timed out may still complete, so "Nothing was changed" was a
prediction. `MutationResult` now separates *applied*, *provably unchanged* and
*uncertain*, and each gets its own sentence.

The recovery advice was also impossible: "clear the Workspace ID field and save"
cannot be carried out, because `SetApiKeyRequest` rejects a blank API key. Every
partial outcome now names **Remove**, which is idempotent — deleting an
already-absent credential succeeds, so a second press goes straight on to the
metadata clear that failed the first time. There is a test that presses it twice.

### 3. `exc_info=True` on three more failure paths

`events.py`, `providers.py` and `preferences.py` still logged raw tracebacks. The
argument that `events.py` was safe because *its own inputs* are fixed did not
address the exception it catches: `unable to open database file:
C:\Users\<account>\AppData\...` is an ordinary sqlite3 message, a
JSONDecodeError quotes the document it failed on — and that document holds the
workspace ID — and `exc_info` renders every full path in the traceback besides.
All five call sites now use `safe_traceback.describe()`, and the AST test covers
ten modules rather than five, `logger.exception()` included.

### 4. A downgrade that was never written was logged as though it had been

`note_runtime_failure()` discarded `store_many()`'s result. On a machine that
cannot write its settings file the log said "downgraded", the preference still
said `verified`, and the dashboard went on offering Claude for the rest of the
session — the exact failure the downgrade exists to prevent, with a log line
claiming otherwise.

Persisting is still attempted first and is still what survives a restart. When it
fails, the observation is kept in this process instead, because "Anthropic
rejected this key thirty seconds ago" is knowledge JARVIS genuinely has. Its
lifecycle is deliberately small enough to state in full: set only by an explicit
live rejection and only ever to a negative state, read only while a key is
configured, cleared the moment the stored credential changes, and lost on
restart — which is correct, because it was never persisted, and saying otherwise
is what this replaces.

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

1. Independent review of this branch — the fourth.
2. The verification gate in the pull request, including two sequential Windows
   Installer acceptance runs.
3. Only then, a real-PC upgrade-in-place by the owner, entering the Workspace ID
   in Settings.

Do not merge, tag, release, deploy, or install JARVIS without a current explicit
instruction.
