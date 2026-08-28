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

- Branch: `claude/jarvis-safe-command-center-v2`
- Pull request: #15 — https://github.com/Dado211207/jarvis-windows-ai-assistant/pull/15
- State: Draft, open, unmerged.
- Base at toolkit adoption: `20fbe1ae55066594f1c0e5b0217dd38526aef486`
- Toolkit-adoption baseline: `d8109650ecd89212e2a8b305e89b76462d483820`
- Query GitHub again before changing, merging or reporting the current head.

## Durable constraints

- Desktop Windows application comes first; the mobile companion is future work.
- Local services bind to `127.0.0.1`; mutation routes require session protection.
- Credentials live in Windows Credential Manager and must never be read back,
  printed, exported or logged.
- Risk decisions belong in `app/core/policy.py`; runtime state belongs in
  `app/core/runtime_state.py`.
- Coding Workspace is isolated from the ordinary assistant and stays inside its
  explicit project boundary.
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
