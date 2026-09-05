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

## Independent review, fourth pass — the Windows backend itself

The corrective work was reviewed again against the *pinned* dependency
rather than against JARVIS's own abstraction, and that found a gap no fake
in this repository had modelled.

### 1. The pinned backend keeps a copy of the secret it replaces

`requirements-windows.txt` pins `keyring==25.7.0`. Its
`WinVaultKeyring.set_password()` reads:

```python
def set_password(self, service, username, password):
    existing_pw = self._read_credential(service)
    if existing_pw:
        # resave the existing password using a compound target
        existing_username = existing_pw['UserName']
        target = self._compound_name(existing_username, service)
        self._set_password(target, existing_username, existing_pw.value)
    self._set_password(service, username, str(password))
```

Note what the code does and its own docstring does not: the copy is
unconditional, not limited to a username collision. With `SERVICE_NAME`
`JARVIS` and `USERNAME` `anthropic_api_key`, replacing the key left two real
Credential Manager entries — `JARVIS` and `anthropic_api_key@JARVIS` — the
second holding the key just replaced. The owner observed exactly those two
with `cmdkey /list`. Nothing in the application could see it:
`_resolve_credential()` returns the plain target first, so the stale secret
was invisible to every read while remaining on disk indefinitely.

JARVIS now treats both names as its own and holds one invariant: after any
settled mutation, only the plain target exists. `_discard_superseded()`
proves three things before deleting anything — the intended survivor is in
place (so removing the copy can never leave the user with no credential),
the compound target exists, and it carries JARVIS's own username, which a
read through keyring establishes because it falls through to a
doubly-compound name otherwise. A compound target belonging to someone else
is left alone, and there is a test for that.

The invariant is applied on first save, on replacement, after a timed-out
write's late reconciliation, after a partial failure, on removal, on full
uninstall, and to the ElevenLabs and OpenAI entries, which reach the same
backend through the same code. An installation that *already* carries the
residue is cleaned by its next ordinary save or removal — no special user
action — and `owned_credential_status()` now sees the compound target, so a
full purge cannot report a clean store while a secret remains.

### 2. A backend exception does not prove the store is unchanged

The same source shows why: `set_password` performs two writes and
`delete_password` walks two targets, so either can mutate and *then* raise.
The previous code recorded the previous value as desired, enqueued nothing,
and returned `MUTATION_UNCHANGED` — a postcondition nobody had observed, and
a half-applied replacement that was never undone.

Now every non-timeout failure reads the store back and compares it against
what was observed *before* the attempt. Only an exact match — plus a proven
absence of any superseded copy, and nothing else in flight — is reported as
unchanged. Anything else is uncertain **and** enqueues a real reconciliation
worker, because updating `_desired_values` alone only tells a future write
what to converge to, and when nothing else is in flight that write never
happens.

### 3. Two real-PC instructions withdrawn

A round-3 report suggested asking the owner to save a key while offline and
check the previous one survived, and to manufacture a metadata-write failure
and press Remove twice. Both are wrong to ask.

The first contradicts the product: `PROVIDER_TIMEOUT` and
`PROVIDER_UNAVAILABLE` are in `key_check._KEY_IS_PROBABLY_FINE`, so an
offline save stores the **new** key and labels it unconfirmed — which is
what checklist step 14e has always said. An unreachable Anthropic is not a
Credential Manager write failure and implies nothing about the credential
already on the machine. The second is not a safe or practical thing to
practise on a real credential.

`docs/physical-pc-checklist.md` now records both exclusions and why, and
`tests/test_workspace_guidance.py` fails if either reappears as an
instruction. Real-PC acceptance stays limited to what an ordinary user does.

## Independent review, fifth pass — the guard that was walked around

The round-4 correction introduced a concurrency defect of its own, and the
review reproduced it deterministically on the exact final source.

### An older failed mutation could overwrite a newer successful one

`_mutate_detailed()`'s failure path called
`_record_desired_if_latest(username, generation, survivor)` — the function
whose entire purpose is to answer "is this older request still the newest
intent?" — and threw the answer away. The next statement called
`_queue_reconciliation()`, which called `_record_desired()`, and
`_record_desired()` unconditionally increments the generation counter and
becomes the newest intent. So the older request's rollback value was
refused as stale and then granted anyway, one line later, by a different
route:

    newer_result       applied
    older_result       uncertain
    final_plain_value  OLD
    targets            ['JARVIS']

The user had been told the newer key was saved. It was, and then an older
save that had *failed* replaced it with the value it had decided to roll
back to.

The same statement produces three different kinds of damage, and all three
are now regression tests in
`tests/test_credential_generation_ordering.py`:

  * an older failed **save** overwrites a newer successful **save**;
  * an older failed **save** resurrects a credential a newer **Remove**
    deleted — the user pressed Remove, was told it was gone, and it came
    back;
  * an older failed **Remove** deletes a credential a newer **save** stored.

**The correction is to the design, not to the symptom.**
`_queue_reconciliation()` no longer mints a generation: it takes one that
`_record_desired_if_latest()` already accepted, so bypassing the guard is
not expressible. A request whose intent has been superseded writes nothing
at all. Cleanup is the single exception, and only because a discard never
touches the plain target: `_cleanup_survivor()` aims it at the newest
recorded intent, so it can neither undo the newer request nor delete a
compound copy on a proof it never made. `MutationResult.superseded` is a
fourth outcome with its own sentence, because the existing one says JARVIS
"has asked for your previous key to be put back" and on this path it
deliberately has not.

Every wait in the new tests is an `Event` or a bounded thread join. The
older request is stopped exactly on entry to `_record_desired_if_latest()`,
which is where the defect lives, so the interleaving is identical on every
machine rather than raced for.

### A docstring that described the opposite of the code

`_discard_superseded()` documented itself as "Called with `_backend_lock`
already held". Every read and delete inside it goes through `_run_isolated`,
which acquires that lock on another thread, so entering it under the lock
would deadlock rather than fail — which is precisely why `_discard_worker`
exists. Corrected in place, with no behaviour change, and backed by a test
that observes the lock's state at every entry across the success, timeout
and failure paths.

## Independent review, sixth pass — the operation, not the store

Round 5 made each *credential* mutation safe against a concurrent one. The
review then asked the next question: what about the *operation*?

### Two successful overlapping operations could disagree about the pair

`credential_pair.save()` is four steps across two stores — snapshot,
credential write, metadata write, rollback — and nothing held them
together. A newer save could complete entirely inside an older one's
window, and then:

    older_result    applied
    newer_result    applied
    final_key       NEWER-KEY
    final_workspace OLDER-WORKSPACE

Every individual part behaved as designed. The older request's credential
write really had landed, so the credential layer correctly owed it
`MUTATION_APPLIED`, and `_cleanup_survivor()` correctly followed the newer
generation. What had never been said is that **"my credential write landed"
is not the same permission as "I may still commit my own description of the
credential"** — and there was nothing above the two stores able to tell
those apart.

Reproduced deterministically in all three orderings, damaging the pair in
two different directions:

| Ordering | Final key | Final Workspace ID |
|---|---|---|
| Save → Save | `NEWER-KEY` | `OLDER-WORKSPACE` |
| Save → Remove | absent | `OLDER-WORKSPACE`, left for the next key to inherit |
| Remove → Save | `NEWER-KEY` | empty — a stored key describing nothing |

The credential store itself was correct every time. The damage was entirely
in the metadata, which is exactly why a guarantee about one store had been
mistaken for a guarantee about the operation.

### The correction

`app/core/ai/credential_transaction.py` serves **one credential-pair change
at a time**, from the snapshot through to the returned `PairOutcome` —
snapshot, credential write, metadata write, rollback, runtime-downgrade
note, outcome. Serialising the operation makes the question disappear: an
older operation cannot still be running once a newer one has committed,
because the newer one could not have started.

Generation counters are kept anyway as a tripwire. `Transaction.is_newest`
is asserted immediately before each metadata write, so a future path that
reaches a store without going through the coordinator is caught by an
assertion rather than by a race. Under the lock it is always true, which is
the point of asserting rather than assuming it.

> **Withdrawn in round 7.** The second sentence above is wrong: a writer
> that never entered the coordinator does not touch the counter
> `is_newest` reads, so the comparison stays true while it does its
> damage. The paragraph is left as written because this is a log of what
> was believed at each round, not a record of having always been right.
> See *5. The tripwire claim was wrong and is withdrawn* below for the
> correction and for what actually catches an escaped writer.

The wait is bounded (`WAIT_SECONDS`, sized against what one transaction can
actually take) and running out of it is a refusal that says nothing was
attempted — which is true, and therefore safe to tell someone.

### Why the UI change is not the fix

The Settings page used to disable only the button that was pressed, so
Save→Remove and Remove→Save overlapped through the ordinary UI. Both
controls now disable together. That is worth doing and it is **not** the
correction: two concurrent `POST /settings/api-key` requests need no button
at all, and FastAPI serves sync routes from a thread pool. The source says
so, and a test asserts the comment is still there.

## Independent review, seventh pass — readers, and who a failure belongs to

Round 6 serialised the writers. The review then asked the two questions
that boundary does not answer: what does a *reader* see, and which
credential does a *delayed failure* belong to?

### 1. A reader could observe a mixed pair

`Brain._provider_config()` assembled the pair from separate store reads —
and `settings.has_anthropic_key` calls `effective_api_key`, so the
credential store was read twice per request. Pausing a successful save
between its two stores put a real chat request in that window:

    observed_key        NEW-KEY
    observed_workspace  wrkspc_OLD

That is a request sent to Anthropic with one key's credential and another
key's workspace. The writer lock does not protect readers, and the Settings
page's button state protects neither Chat nor a direct API call.

`app/core/ai/credential_view.py` now builds one immutable `CredentialPair`
while holding a **read gate** on the coordinator, and every reader takes it:
`Brain._provider_config()` (and the Coding Workspace through it),
`GET /settings/api-key-status`, and `anthropic_credential_state()` — which
is now a thin wrapper over `credential_state_for(pair)` so a caller that
already has a snapshot describes *that* pair rather than looking again.

There is deliberately **no revision-keyed cache**. A cache would be stale
for any write that did not come through the coordinator, and "which writes
go through the coordinator" is enforced by a test rather than by the type
system — not something a reader should depend on. Building every time also
costs *less* than the code it replaces, which read the credential twice.

No lock is held across a network request: a snapshot is taken, the gate is
released, and only then does anything talk to Anthropic.

### 2. A delayed failure downgraded the credential that replaced it

Both provider failure paths called `note_runtime_failure(provider.name,
exc.category)` with nothing identifying which credential made the failed
request. So a request carrying the old key, returning `PROVIDER_AUTH` after
the user had saved a new one, marked the new, working credential
`verification_failed`:

    current_state_before_old_failure  verified
    failed_request_key                OLD-KEY
    current_state_after_old_failure   verification_failed

This also disproved the comment in `providers.py` claiming the runtime note
"cannot outlive the key it describes". `save()` does clear it — and the
delayed old request recreated it afterwards. Clearing on save orders
nothing, because the failure arrives later than the save.

The snapshot's **non-secret revision** now travels on `ProviderConfig`, both
failure paths pass it, and a rejection is discarded unless it still
describes the stored pair. The process-local downgrade is stored against its
revision too, so a session-only note cannot describe a later credential
either. The revision is a counter — never a hash of a key, never derived
from a secret — so it is safe in a log line in a way the key never is.

A lock inside `note_runtime_failure()` would not have worked: the delayed
failure could simply wait for the save to finish and then be just as wrong.
Only identity settles it.

### 3. Ordering at the HTTP boundary

`POST /settings/api-key` verifies with Anthropic **before** it may store
anything, so round 6's tests — which called `save()` directly — proved
nothing about two concurrent requests. Two of them could verify out of
order and reach the coordinator in the opposite order to the one the user
made them in.

The rule, chosen and documented before the fix: **the request admitted
later wins.** Admission, not completion — that is the order the person
acted in, and the only one they can observe. Each request takes an intent
when it is admitted, verifies, and presents the intent to the coordinator;
an older request whose check finished late is refused with a truthful
message rather than landing on top of a newer accepted save. Remove is
ordered by the same rule, because it changes the same credential.

### 4. The busy outcome reported facts it did not have

`_busy()` hard-coded `stored=False` for a save, `stored=True` for a remove
and `consistent=True` for both. Those describe the *installation*, and a
request that never started observed neither store — least of all in the
case this outcome occurs in, where the transaction in front may be part-way
between the credential and its metadata. Round 6's test held a transaction
that touched neither store, which is exactly why the hard-coded answers
looked true. Both fields are now `Optional[bool]` and `None` there, the API
model carries the null through, and the page treats it as unknown.

### 5. The tripwire claim was wrong and is withdrawn

`Transaction.is_newest` is always true while the gate is held, and a writer
that never entered the coordinator does not touch the counter it reads — so
it cannot detect one. The assertion is kept as an internal invariant and
described as one. What actually catches an escaped writer is a structural
test that walks every module under `app/` and fails if anything outside a
named allowlist calls the credential mutators.

### 6. `current()`'s docstring described the cache that was removed

Found while assembling this round's evidence, and the same species of
defect as round 5's `_discard_superseded()` docstring: a docstring
asserting the opposite of its own code.

An early draft of `credential_view.current()` looked the snapshot up by
revision and returned it when the number still matched. That was removed
before the commit — such a cache is stale for every write that does not go
through the coordinator (`ANTHROPIC_API_KEY` in the environment,
`ownership.py`'s uninstall sweep, a test seeding the stores), and "every
write goes through the coordinator" is enforced by a test rather than by
the type system, which is a bad thing for a *reader* to depend on. The
body's comment explained the removal. The docstring above it still said
"the published snapshot is reused while its revision still matches, so an
ordinary request touches no store at all" — a promise the function does
not keep, sitting where a reader would look first.

The docstring now says what the code does: both stores are read every time
under the gate, and the published snapshot is only the fallback for a gate
the reader could not enter. `tests/conftest.py`'s note about why it
invalidates the snapshot said the same wrong thing and is corrected too.

Pinned by behaviour rather than by a string search, because the wrong
claim was a claim about behaviour:
`test_a_second_read_at_the_same_revision_still_reads_the_stores` changes
the credential store without moving the revision and asserts the next read
sees it. Verified by temporarily reinstating the fast path, at which point
it fails with the stale key (`- sk-ant-api03-NEW-key-just-saved / +
sk-ant-api03-OLD-key-in-flight`) while the other nine still pass.

### 7. The reproduction test did not reproduce anything

Found in the final verification pass of this round, by re-running the new
tests against the pre-fix head instead of trusting the number recorded
when they were written. **The headline test for blocker 1 passed against
the code it was written to convict.**

`test_a_reader_never_sees_one_requests_key_with_another_requests_workspace`
parks a save between its two stores, starts a reader on another thread,
and then releases the save. `Thread.start()` returns as soon as the thread
exists — nothing made the reader's *read* land inside the paused window.
On this machine it lost that race every time: it read after the save
completed, saw a coherent NEW/NEW pair, and passed. The defect was real
the whole time; reading on the main thread inside the window shows it
immediately:

    store credential at pause : NEW
    store workspace at pause  : OLD
    reader observed key       : NEW
    reader observed workspace : NEW      <- read after the release
    mixed_pair                : False

    (read strictly inside the window)
    reader observed key       : NEW
    reader observed workspace : OLD
    mixed_pair                : True

This is the repository's own clap-flake lesson in a second place: *a phase
is not a receipt*. `state === "calibrating"` did not mean a microphone was
open, and a started thread does not mean a read has happened. Both times
the test waited for the thing that would produce the evidence rather than
for the evidence.

Auditing the rest of the round's tests the same way found two more of the
same species, both fixed here:

* `test_the_status_endpoint_never_reports_a_mixed_pair` read through
  `credential_view.current()`, which does not exist on the pre-fix head —
  so it "failed" there with an `ImportError`. A module that has not been
  written yet is not evidence that a defect was detected. It now drives
  the real `GET /settings/api-key-status`, which exists on both heads and
  reports `configured=True workspace_configured=False` on the old one.
* **Blocker 2 had no reproduction that could run on the code it was
  about.** `test_a_delayed_failure_from_the_old_key_does_not_downgrade_
  the_new_one` captures a snapshot and attributes the failure to its
  revision — both introduced by this round — so it stopped at the same
  `ImportError`. `test_a_delayed_rejection_that_names_no_credential_
  downgrades_nothing` now makes the two-positional-argument call the old
  code made, which is the whole difference: the old code downgraded
  whatever was stored when the rejection landed, the new code discards a
  rejection naming no credential. On `67f0205` it convicts with
  `verification_failed` where `verified` is required.

`test_credential_request_ordering.py` was audited the same way and needed
nothing: all six of its pre-fix failures are substantive assertions.

`_reader_can_no_longer_see_the_finished_save()` is the fix: it blocks
until releasing the save can no longer change what the reader saw, and the
two ways that becomes true are exactly the difference under test — before
the correction the read is ungated and simply completes (`finished`);
after it the reader parks on the read gate before touching a store
(`wait_for_waiters(1, …)`, which already existed for this purpose and
which `read_gate` already participates in). No production code changed, no
sleep, and no timeout used as a positive signal.

Verified in both directions, 25 runs each: at `7964947` all 19 round-7
tests pass 25/25; at `67f0205` both reproductions convict 25/25, with

    a reader was given one request's key with another request's Workspace ID:
    key=NEW workspace=OLD

    the status endpoint described a key from one request and a workspace from
    another: configured=True workspace_configured=False

The round's failing-first figure was re-measured rather than quoted, with
the final test files run against `67f0205` and *its own* `conftest.py`.
**17 of 20 fail there — and 11 of those 17 are substantive assertions.**
The other six cannot be anything else: they test `credential_view`, which
the correction introduces, so they stop at an `ImportError`. That is
normal for tests of a new component and is reported as such rather than
counted as detection, which is the whole point of separating the two
numbers.

## Independent review, eighth pass — the gap between the check and the write

Round 7 gave every request the revision of the pair it was built with and
made a rejection carrying a stale revision be discarded. That fixed
*attribution*. The eighth review found it had not made the decision and
the write one act.

### The defect

`note_runtime_failure()` read the current revision, compared it, and then
wrote — with nothing holding the coordinator across the two:

    if credential_revision != credential_view.current_revision():   # read
        return
    _remember_runtime_downgrade(...)                                # write
    store_preferences({VERIFICATION_PREFERENCE: downgraded})        # write

A Save can complete in that window. The comparison has already been made,
against a number that was true when it was read and is not true when it is
used, so it still passes — and the rejection of the *previous* key writes
`verification_failed` over the credential that replaced it. Reproduced on
the unmodified head by pausing inside the revision check itself:

    in_flight_revision              0
    new_save_outcome                applied
    revision_after_new_save         1
    state_before_old_failure_resumes verified
    current_key                     NEW-KEY
    current_workspace               wrkspc_NEW
    persisted_state_after_failure   verification_failed
    reported_state_after_failure    verification_failed
    runtime_note_for_new_revision   None

The last line is round 7 working: the process-local note stayed correctly
revision-scoped. The persisted preference did not, and the next
`credential_view.current()` reads it back and hands it to the new revision.

**Why round 7's own test missed it.** It ran the whole Save and *then*
called `note_runtime_failure()`, so the first comparison already saw the
new revision and rejected the stale failure. The window was never entered.
Catching this needs a pause inside the check — the same lesson as round 7
section 7, one layer further in: a test that never reaches the window
proves nothing about the window.

### The rule, decided before the code

**The revision identifies which credential pair is stored** — the key and
its Workspace ID together. It advances when a transaction changes that
pair, because afterwards it is a different credential. A verification-state
change describes the *same* pair differently: only what was last observed
about it has moved. So it must **not** advance the revision.

That rule is what makes the obvious implementation wrong. Wrapping the
downgrade in `begin()` would increment the revision *before* the expected
one could be checked, so every legitimate rejection of the current
credential would compare against a number that had already moved and be
discarded as stale — the opposite failure, and a silent one: nothing would
ever be downgraded again. `test_two_rejections_of_the_same_credential_are_
both_honoured` and the three `PROVIDER_AUTH`/`WORKSPACE_REQUIRED`/`BILLING`
cases exist to catch exactly that.

### The correction

`credential_transaction.pair_state_gate()` holds the gate without minting a
transaction and without moving the revision — `read_gate()`'s mechanism,
named for a different purpose, both now sharing `_hold()`.
`note_runtime_failure()` takes it, then re-validates the revision **while
holding it**, and only then writes the runtime note and the preference.

The pre-gate comparison is kept as a cheap early-out and documented as
nothing more: it can only end in a skip, never in a write, so an answer
that goes stale between it and the gate costs nothing.

The wait is bounded (`DOWNGRADE_WAIT_SECONDS = 5.0`) and running out of it
records **nothing**: a credential change is in progress, what is stored is
being decided right now, and a rejection that cannot be attributed safely
is better dropped than guessed at. A real rejection of whatever ends up
stored recurs on the next request. No network call happens under the gate —
the provider request that produced the failure finished before this runs.

Lock order is unchanged: `_gate` then `_runtime_downgrade_lock`, which is
already the order `credential_pair.save()` takes them in through
`clear_runtime_downgrade()`, so there is no inversion to create.

### `_build()` claimed a fact it had not established

Audited in the same pass. `CredentialPair.readable` is documented as
"False when the credential store could not be read coherently at all —
distinct from 'there is no key'", and `_build()` hard-coded it `True`.
`settings.effective_api_key` cannot tell the two apart either: it answers
`""` for both.

The fact was already available. `credentials.stored_api_key_snapshot()`
returns `(store_reached, value)` — the pair the uninstaller depends on for
this very reason, since collapsing them there would delete a data folder
while a secret still existed. `_build()` now carries it, and
`configured` is no longer set from an unreadable store: not being able to
read is not evidence that nothing is stored.

Environment precedence is now explicit rather than inherited. When
`ANTHROPIC_API_KEY` is set it wins and the credential store is not consulted
at all, so `readable` is true because the question never arose — proven by
a test that makes `stored_api_key_snapshot` raise if it is called.

### Verification

**4 of 14 new tests fail on the unmodified `fdef269`**, all on substantive
assertions and none on an import: the TOCTOU itself, the Save window, the
Remove window, and the `readable` claim. The other ten pass on both heads —
they are the guards that the correction must not break, including the three
category cases and the two-rejections-of-one-key case that would catch a
revision that moved when it should not have.

## Independent review, ninth pass — the file underneath all of it

Rounds 6 to 8 built a boundary around the credential pair: one change at a
time, one coherent snapshot for readers, a revision that says which pair a
failure belongs to, and a gate held across the check and the write. All of
it persists half its state through `preferences.json`, and **that file had
no boundary at all.**

### The defect

`preferences.store_many()` was an unguarded read-modify-write over one
shared document: load the whole thing, merge the requested keys, write a
shared `preferences.json.tmp`, replace the file. Every writer merged into
*its own* snapshot and then replaced the **entire** document, so a writer
that loaded before another committed silently restored whatever that other
had changed — and both returned `True`.

The credential transaction cannot help, because it is not in the way. It
serialises credential Save/Remove/state; this file is also written by the
preferred name, provider selection, voice settings, clap settings and
local-AI ownership, none of which enter that gate. Observed on `e3523d2`
against the real production module:

    both_calls_reported_success   {'credential': True, 'unrelated': True}
    final_preferred_name          After
    final_workspace               wrkspc_OLD
    final_state                   verified
    lost_credential_metadata      True

In `credential_pair.save()`'s ordering that is a new key in Windows
Credential Manager, the previous Workspace ID and verification state on
disk, and `PairOutcome(APPLIED)` already returned to the user — the
guarantee three rounds were spent building, defeated from outside the
boundary built to protect it. Somebody changing their display name is
enough to do it.

**The shared temporary file made it worse than a lost update.** Two writers
wrote and replaced the same `preferences.json.tmp`, and one run left this
on disk:

    {
      "preferred_name": "Before",
      "anthropic_workspace_id": "wrkspc_01OLDworkspaceidvalue",
      "anthropic_key_state": "verification_failed"
    }y": "af_heart"
    }

A complete document with the tail of another writer's document after it.
`load()` answers `{}` for that, so *every* saved preference is gone — not
one update lost, all of them.

### The invariant

Once a successful `store_many()` returns, its update may not be lost by a
concurrent successful update to different keys. A reader sees the complete
old document or the complete new one, never a partial write.

### The correction

`_write_lock` serialises the whole operation — load, merge, write, replace —
at `store_many()`, which is the single entry point every writer already
uses (`store()` delegates to it, and a test walks the AST to prove no
second write path exists). Fixing this inside `credential_pair` or the
API-key routes would have left every other writer able to reproduce it.

The temporary file is now **unique per write**
(`preferences.json.<pid>.<uuid>.tmp`), so no two writers can ever share
one — including across processes, and including a stale file left by a
crash.

**Process scope, established rather than assumed.** Every runtime writer
runs in the server child: the FastAPI routes, `voice/engines.py`,
`voice/tts.py`, `voice/clap.py`, `core/local_ai_install.py`,
`core/ai/credential_pair.py` and `core/providers.py`. The tray parent and
the window child only read — `launcher/gui.py` uses `get` — a single
instance is enforced by `launcher/instance_lock.py`, and
`--uninstall-cleanup` runs after the application has been told to stop. A
process-local lock is therefore sufficient today, and the unique temporary
name means even an unexpected second process could not tear a document.

**Lock order.** `_gate` -> `_runtime_downgrade_lock` -> `_write_lock`.
`save()` holds the coordinator and then writes metadata;
`note_runtime_failure()` holds the coordinator, then the runtime-note lock,
then writes. So this lock must be a leaf, and
`test_the_write_lock_is_a_leaf_and_can_deadlock_with_nothing` proves it
from the module's imports rather than from a substring search — the first
version of that test failed because `anthropic_workspace_id` and
`anthropic_key_state` are *preference key names* in `STORABLE_KEYS`, which
a text search cannot tell apart from an import of the SDK. Nothing
unbounded runs inside the lock: no network, no provider call, only JSON and
one atomic replacement.

### A second loss path, inside the same function

`store_many()` built its merge on `load()`, which answers `{}` both for a
file that does not exist and for one it could not read. That is right for a
reader and wrong for a writer: merging into `{}` and replacing turns an
unreadable document into a deleted one, so a single transient sharing
violation could erase every saved preference including the credential
metadata.

`_load_for_update()` separates the three cases. A missing file is safe to
replace — nothing to lose. A file whose *content* is unparseable is also
safe, because every reader already sees `{}` for it and overwriting
recovers rather than destroys. A file that exists and could not be **read**
is refused: losing this one update is better than losing all of them.

This was not in the round's brief. It is reported rather than folded in
quietly, because it is a behaviour change in the same function and a
reviewer may reasonably want it separated.

### Two defects the Windows job found that the Linux gate could not

Both were mine, both were in the round-9 commit, and both are corrected in
the same change.

**1. `os.replace` fails on Windows while a reader holds the file.** The
first version of this correction said readers could stay lock-free
"because replacement is atomic". That is true about *tearing* and wrong
about *failing*: `MoveFileEx` raises a sharing violation —
`PermissionError` — while another handle is open on the destination, and
`load()`/`get()` are deliberately lock-free and constant (`/health`,
`/providers`, every chat request takes a credential snapshot). So an
ordinary status read could make a correct write return False and lose a
credential-metadata update. The Windows Build job on `c99332a` said so
exactly:

    Could not write the preferences file. types=PermissionError
    frames=[app/core/preferences.py in _write_document | pathlib.py in replace]

`_replace_atomically()` now retries a `PermissionError` a bounded number of
times with a short backoff — a reader's handle lives for microseconds — and
still returns False if every attempt loses. Only `PermissionError` is
retried; a full disk will not fix itself in a hundred milliseconds. Two
tests cover it on any platform by failing the replacement deliberately: one
that it recovers, one that a replacement which never succeeds is still
reported as a failure and leaves no temporary file behind.

The local Linux gate could not have caught this. On Linux the replacement
simply succeeds, whoever is reading.

**2. A test that outlived its own failure.** The reader in
`test_a_reader_never_observes_a_partial_document` was a daemon thread
stopped after the assertions. When the first write failed on Windows the
assertion raised, `stop.set()` never ran, and the thread kept reading for
the rest of the session — against the *shared* preferences file once the
fixture's monkeypatch was undone, holding a handle open. Five entirely
healthy tests then failed with sharing violations:
`test_preferred_name_and_close_action` (two), `test_provider_selection`,
`test_tts` and `test_voice_output`. The teardown is now in a `finally`
with an explicit "did not outlive the test" assertion.

### The teardown abort, attributed

One full-gate run reported its totals and then aborted at interpreter
teardown with `terminate called without an active exception` (exit 134).
It is **not** the preferences change. Running the voice suites directly
printed the cause immediately before the abort:

    [E:onnxruntime:, sequential_executor.cc:671 ExecuteKernel] Non-zero
    status code returned while running ReduceMean node.
    Name:'/decoder/decoder/generator/resblocks.3/adain1.0/ReduceMean_2'
    Status Message: GetElementType is not implemented

That is onnxruntime tearing down the Kokoro neural-voice decoder, in a
suite this round does not touch. It is intermittent — one occurrence in
five runs of the corrected tree — and is recorded rather than argued away
statistically, because a direct cause beats a run count. It belongs to the
voice stack and needs its own decision.

### Verification

**5 of 9 new tests convict `e3523d2`, 25 runs out of 25**, all on
substantive assertions; all 9 pass on the correction, 25 out of 25.

The tests drive the real `app/core/preferences.py` against a real temporary
`preferences.json` — not the in-memory `_Preferences` double the credential
suites use, because the defect lives in the file path itself.

An earlier draft of them was **not** deterministic. Moving both writers
onto threads left the order after the release to the scheduler, and two
tests convicted only by luck. `_second_writer_can_no_longer_be_reordered()`
waits for whichever of two things actually happens — the second writer
completes (no lock) or parks on the lock (corrected) — which is the round-8
barrier lesson applied to a new place: wait for the evidence, not for the
thread that will produce it.

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

1. Independent review of this branch — the eighth.
2. The verification gate in the pull request, including two sequential Windows
   Installer acceptance runs.
3. Only then, a real-PC upgrade-in-place by the owner, entering the Workspace ID
   in Settings.

Do not merge, tag, release, deploy, or install JARVIS without a current explicit
instruction.
