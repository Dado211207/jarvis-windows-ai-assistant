# Dado toolkit adoption

This repository uses the public `Dado211207/dado-claude-toolkit` marketplace.

- Toolkit source: `Dado211207/dado-claude-toolkit`
- Reviewed toolkit commit: `da8276e03b54d521462a0a15861d58227fb1546a`
- Enabled plugins: `dado-core`, `dado-ui-design`, `dado-python-windows`,
  `dado-release-safety`
- UI-design upstream snapshot: `nextlevelbuilder/ui-ux-pro-max-skill` at
  `8bd29e775453ebcae52b6e6514fbf134df0c5770` (MIT)
- Adopted in: JARVIS Draft PR #15

## Two loading paths

`.claude/settings.json` declares the four external plugins. Claude Code documents
that path, but an account or environment may still need to trust the marketplace.

To make cloud work independent of that trust step, the relevant 19 skills and nine
agents are also committed under `.claude/skills/` and `.claude/agents/`. Those are
reviewed snapshots from the toolkit commit above. Their names are intentionally
unnamespaced (`/orient`, not `/dado-core:orient`). Refresh them deliberately from a
reviewed toolkit commit; never overwrite local changes blindly.

The `ui-ux-pro-max` fallback includes its local, standard-library-only search data
and scripts plus the upstream MIT notice. Its examples use the repository-relative
`.claude/skills/ui-ux-pro-max/scripts/search.py` path because
`CLAUDE_PLUGIN_ROOT` is available only when the external plugin loaded.

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
