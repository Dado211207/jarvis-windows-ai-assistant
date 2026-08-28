# Dado toolkit adoption

This repository uses the public `Dado211207/dado-claude-toolkit` marketplace.

- Toolkit source: `Dado211207/dado-claude-toolkit`
- Reviewed toolkit commit: `81796b8d5d35bef723fc180f4dd98c61f90e2052`
- Enabled plugins: `dado-core`, `dado-python-windows`, `dado-release-safety`
- Adopted in: JARVIS Draft PR #15

## Two loading paths

`.claude/settings.json` declares the three external plugins. Claude Code documents
that path, but an account or environment may still need to trust the marketplace.

To make cloud work independent of that trust step, the relevant 18 skills and nine
agents are also committed under `.claude/skills/` and `.claude/agents/`. Those are
reviewed snapshots from the toolkit commit above. Their names are intentionally
unnamespaced (`/orient`, not `/dado-core:orient`). Refresh them deliberately from a
reviewed toolkit commit; never overwrite local changes blindly.

The plugin hooks are not copied. If the external plugin does not load, the committed
permission denies and the repository rules in `CLAUDE.md` still apply, but no hook
should be described as active.

## Precedence and authority

1. Current user instructions and platform safety rules.
2. This repository's `CLAUDE.md` non-negotiable product and release rules.
3. `docs/ai/PROJECT_STATE.md` as tracked context, never as authorization.
4. Toolkit skills and agents.

No toolkit file authorizes a merge, release, tag, signing operation, deployment,
credential access, or change outside this repository. Those actions still require a
specific instruction in the current task.
