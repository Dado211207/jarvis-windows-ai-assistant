# Project state

This is a public, tracked orientation file, not cross-session memory. This file is
not authorization. Code and current user instructions take precedence if it becomes
stale. Never add secrets, personal data, absolute local paths, or private messages.

## Purpose

JARVIS is a local-first Windows AI assistant with a native desktop shell, local web
dashboard, voice input/output, approval-gated actions, memory, diagnostics and a
separate safe Coding Workspace for the owner's own repositories.

## Distribution state

- Version in source: `0.2.0-rc1` (`app/__init__.py`, `packaging/jarvis.iss`).
- Production/public release: not released.
- Current installers are unsigned CI acceptance artifacts, not a user release.
- Real microphone, audible voice quality, SmartScreen and full human setup remain
  real-PC acceptance work; automation cannot mark them passed.

## Active work

- Branch: `claude/anthropic-workspace-id`
- Base: `main` at `884c737d465afddeaee31ba99567345861602026`
- Purpose: JARVIS could not authenticate with an Anthropic **identity-linked**
  API key (a personal or service account key that is not scoped to one
  workspace). Anthropic requires the `anthropic-workspace-id` header for those,
  and JARVIS stored only a key. Reproduced on the owner's real Windows 11 PC and
  independently outside JARVIS. See `docs/ai/WORK_LOG.md`.
- A second independent review accepted the header work and found **six
  blockers** around it: unactionable Default Workspace guidance, a non-atomic
  two-store write with no compensation, no Logs event on the key-verification
  path, a workspace ID reachable through `to_safe_error`'s raw exception log, a
  transient check recorded as a rejection, and stale first-run copy. All six are
  corrected on this branch, each with regression tests written against the
  failure first.
- A **third** review found four more, all in the corrective commit itself, and
  all four are now corrected:
  1. `credentials._mutate()` reconciled every failed non-None write to
     *absence*, so a replacement that failed or timed out **deleted the key it
     was replacing** while the route answered "Nothing was changed". The
     reconciliation target is now the proven previous value, and a store that
     cannot be read is not written to at all.
  2. Removal reported "unchanged" for a delete that may still land, and told
     the user to clear the Workspace ID and save — impossible, because
     `SetApiKeyRequest` refuses a blank key. Removal is now truthful about what
     it established, and every partial outcome names Remove, which is
     idempotent and finishes the metadata cleanup.
  3. `exc_info=True` survived in `events.py`, `providers.py` and
     `preferences.py`. SQLite and filesystem exceptions quote AppData paths
     containing the account name, so all five are now `safe_traceback.describe`.
  4. `note_runtime_failure()` discarded `store_many()`'s result and logged a
     downgrade that may never have been written.
- A **fourth** review found a Windows-backend gap no fake had modelled, and
  both parts are now corrected:
  1. `requirements-windows.txt` pins `keyring==25.7.0`, whose
     `WinVaultKeyring.set_password()` copies any existing credential to
     `{username}@{service}` **before** writing the replacement — and does so
     unconditionally, not only on the username collision its own docstring
     describes. Replacing the Anthropic key therefore left two real
     Credential Manager targets, `JARVIS` and `anthropic_api_key@JARVIS`,
     the second holding the key just replaced; the owner observed both with
     `cmdkey /list`. Reads never showed it, because `_resolve_credential()`
     returns the plain target first.
  2. A backend exception was classified as `MUTATION_UNCHANGED`. That
     backend's `set_password` performs two writes and its `delete_password`
     up to two deletes, so either can mutate the store and *then* raise.
- A **fifth** review found a concurrency defect inside the round-4
  correction, and it is now fixed: **an older failed mutation could
  overwrite a newer successful one.** `_mutate_detailed()`'s failure path
  asked `_record_desired_if_latest()` whether its rollback value was still
  the newest intent — the function that exists for exactly this — and then
  discarded the answer, because the next statement called
  `_queue_reconciliation()`, which called `_record_desired()` and minted a
  brand-new *newest* generation for the stale request's value. The guard ran,
  correctly said no, and was walked around one line later. Reproduced
  deterministically in all three same-credential orderings: a stale failed
  save overwriting a newer save, resurrecting a credential a newer Remove
  had deleted, and a stale failed remove deleting a key a newer save had
  stored. The accepted generation is now passed into the reconciliation
  worker, a superseded request writes nothing, cleanup follows the newest
  recorded intent, and `MutationResult.superseded` gives that outcome its own
  truthful message instead of promising a rollback nobody performed.
- A **sixth** review found a broader race across the *whole* two-store
  operation, and it is now fixed: **two successful overlapping operations
  could leave the key and its Workspace ID describing different requests.**
  `credential_pair.save()` is four steps — snapshot, credential write,
  metadata write, rollback — and nothing held them together, so a newer save
  could complete inside an older one's window. The older save's credential
  write really had landed, so the credential layer correctly owed it
  `MUTATION_APPLIED`; it then took that as permission to commit its own
  Workspace ID over the newer request's. Reproduced deterministically in all
  three orderings, in two directions: Save→Save left `NEWER-KEY` with
  `OLDER-WORKSPACE`; Save→Remove left a removed key's Workspace ID for the
  next key to inherit; Remove→Save left a stored key describing nothing.
  `app/core/ai/credential_transaction.py` now serves one credential-pair
  change at a time, from the snapshot through to the returned `PairOutcome`,
  with a bounded wait and a truthful refusal. The Settings page disables
  Save and Remove together as defence in depth, and says in the source that
  it is not the correction.
- State: awaiting a further independent review. Nothing installed, merged,
  tagged, released or deployed. **All installer evidence from every previous
  head is superseded** — each corrective commit changes runtime,
  Setup/Settings, credential persistence and packaged behaviour.
- Query GitHub again before changing, merging or reporting the current head.

## Completed work

- Pull request #15 (`claude/jarvis-safe-command-center-v2`) is **merged** —
  squash-merged into `main` on 2026-08-28 as `a6eaeac`. The source branch still
  exists at `7b1543cd639ac4ecf7c4eaa45dd19512bec63f16` and must not be modified.
- Pull request #18 (`claude/pre-install-audit-hardening`) is **merged** —
  squash-merged into `main` on 2026-08-31 as
  `884c737d465afddeaee31ba99567345861602026`. Its post-merge CI, Windows Build
  and a genuinely non-skipped Windows Installer job were all green at attempt 1.
- **Real-PC acceptance reached the AI provider and stopped there.** WebView2
  151.0.4129.59, checksum match, install, native window rendering the real
  dashboard, healthy database and health, and a working Anthropic account all
  passed. An identity-linked API key then failed every request — the defect the
  active branch fixes.
- Pull request #17 (`claude/fix-v02-inno-installer`) is **merged** —
  squash-merged into `main` on 2026-08-30 as
  `22e419da530b18d37f9d9aa5416aed5dc3b28894`. It repaired the v0.2 installer
  build. Its source branch still exists at `109e17a` and must not be modified.
- The post-merge runs on `22e419d` were all green at attempt 1, and the Windows
  Installer job genuinely **ran** rather than skipping — the specific failure
  mode two of PR #17's four defects were about. A merged pull request is not the
  same as a verified installer; this one is verified.
- **The `msedge.exe` survivor remains unreproduced and unfixed.** Its opt-in
  diagnostics scaffolding is deliberately still in the tree. Do not weaken
  `survivors == []`, raise a timeout, add a retry, or sweep by image name.

## Durable constraints

- Desktop Windows application comes first; the mobile companion is future work.
- Local services bind to `127.0.0.1`; mutation routes require session protection.
- Credentials live in Windows Credential Manager and must never be read back,
  printed, exported or logged.
- Risk decisions belong in `app/core/policy.py`; runtime state belongs in
  `app/core/runtime_state.py`.
- Coding Workspace is isolated from the ordinary assistant and stays inside its
  explicit project boundary.
- The Anthropic API key lives in Windows Credential Manager; the **Workspace ID**
  is account metadata in `preferences.py`, never the credential store, and
  neither is ever returned by an endpoint, logged or put in a diagnostic. An
  identity-linked key without its workspace cannot authenticate — see
  `app/core/ai/workspace.py`.
- The key and its metadata are written through `app/core/ai/credential_pair.py`
  and nowhere else. Two stores are involved, so success is never reported unless
  both reached the intended state; a failed metadata write rolls the credential
  back, and a failed rollback is reported precisely rather than as success. An
  unreadable credential snapshot never authorises a destructive rollback — and
  never authorises a *replacement* either, because a write that could not be
  undone is a write that must not be started.
- **One logical secret occupies one credential target.** The pinned Windows
  backend keeps a copy of what it replaced under `{username}@{service}`, so
  JARVIS owns two names per credential and holds the invariant that only the
  first exists after any settled mutation. Nothing is deleted until the
  intended survivor is proven in place and the copy is proven to carry
  JARVIS's own username — `credentials._discard_superseded()`.
- **An exception is not a postcondition.** A backend that mutated the store
  and then raised is never reported as having changed nothing; the store is
  read back and compared against what was observed *before* the attempt, and
  anything else is uncertain plus an actively submitted reconciliation
  worker. Recording a desired value is not restoring one.
- **A failed credential write reconciles to the value that was proven to be
  there, never to absence.** The old and new Anthropic keys are the same
  Credential Manager entry, so "clean up after a failed save by deleting it"
  destroys a working key on every replacement. `credentials._mutate_detailed()`
  reads the entry first and that read is a precondition of writing at all.
- **"Nothing was changed" is a postcondition, never a consolation.**
  `MutationResult` separates *applied*, *provably unchanged* and *uncertain*; a
  call that never returned may still complete, and no message may describe it
  as having changed nothing. Every recovery instruction must be an action the
  UI can actually perform — Remove is idempotent and is the one named.
- **A credential-pair change runs alone, and the guarantee is about the
  operation rather than either store.** `save()` and `clear()` hold
  `credential_transaction.begin()` across the snapshot, the credential
  write, the metadata write, the rollback, the runtime-downgrade note and
  the returned outcome. A credential write that landed is never, by itself,
  permission for an overtaken operation to commit its own Workspace ID —
  and `Transaction.is_newest` is asserted before either metadata write as a
  tripwire for a future path that escapes the coordinator. A request that
  cannot get in is refused with "nothing was changed", which is true; it is
  never queued indefinitely. UI button state is defence in depth and must
  never be presented as the boundary: two direct API calls need no button.
- **Latest intent wins, and the check that establishes it is the only thing
  allowed to grant one.** `_record_desired_if_latest()` returning `None` means a
  newer request owns the credential; nothing the older request does afterwards
  may create a newer generation, queue a write, or roll anything back. Cleanup
  is the one exception and only because a discard never writes the plain
  target: it follows the newest recorded intent (`_cleanup_survivor`) so it can
  neither undo nor misclassify the request that overtook it. `MutationResult`
  has a fourth outcome, `superseded`, precisely so no message claims the
  previous value was put back on the one path where it deliberately was not.
- A live rejection JARVIS could not write to disk is still applied for the rest
  of the process (`providers.runtime_downgrade()`), is only ever a *negative*
  state, and is dropped the moment the credential it describes changes. It is
  never claimed to have been persisted when it was not.
- Provider status has five states. "A credential exists" is never reported as
  "chat is available"; only a credential the provider actually answered for is.
  A check that could not run is `configured_unverified`, never
  `verification_failed` — an offline machine has not rejected anything.
- No raw exception is written to any log on a credential, provider or
  preferences failure path — not `exc_info`, not `logger.exception()`, not
  `str(exc)`. `app/core/safe_traceback.py` describes one instead (type chain and
  trimmed frames), because Anthropic's documented 404 for an inaccessible
  workspace quotes the workspace ID inside the response body, and a SQLite or
  filesystem error quotes an AppData path containing the account name. Ten
  modules are held to this by an AST test in
  `tests/test_credential_replacement_safety.py`.
- Every surface that names the Console's Workspaces table must also say the
  Default Workspace is not in it (`tests/test_workspace_guidance.py`).
- `GET /desktop/ready` proves four *process* facts and is never evidence that
  WebView2 rendered the dashboard. pywebview's `loaded` event is not that
  evidence either — it fires on an error page too. Anything claiming visible
  rendering must prove it at the page.
- No tag, release, signing, merge or deployment without a current explicit request.

## Protected areas

- `CLAUDE.md` non-negotiable rules — preserve unless the owner explicitly changes
  the corresponding product decision.
- `requirements*.txt`, packaging and installer workflows — dependency or release
  changes require their full Windows/installer verification path.
- Secret redaction, policy, approvals and workspace containment — never weaken to
  make a test pass.
- Existing user data and migrations — no destructive reset or silent data loss.

## Verification commands

```bash
JARVIS_LOG_LEVEL=WARNING JARVIS_DB_PATH=/tmp/jarvis_gate.db pytest
pytest
python -m compileall app db
```

- Windows packaging and installed-product checks run through the repository's
  existing Windows workflows and scripts.
- Physical audio, microphone, UI feel, antivirus and SmartScreen checks remain
  manual even when CI is green.

## Toolkit adoption

- Source and provenance: `.claude/TOOLKIT.md`.
- External plugins are declared in `.claude/settings.json`.
- Repository fallbacks: 19 skills and nine agents under `.claude/`, including the
  local `ui-ux-pro-max` catalogue and search runtime.
- Existing JARVIS rules win over a generic toolkit recommendation.
