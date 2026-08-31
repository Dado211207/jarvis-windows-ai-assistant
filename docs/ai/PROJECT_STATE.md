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
- State: awaiting independent review. Nothing installed, merged, tagged,
  released or deployed.
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
- Provider status has four states. "A credential exists" is never reported as
  "chat is available"; only a credential the provider actually answered for is.
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
