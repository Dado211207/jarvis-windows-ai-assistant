# Third-party provenance

This plugin vendors a reviewed runtime snapshot of **UI UX Pro Max**.

| Field | Value |
| --- | --- |
| Upstream repository | `https://github.com/nextlevelbuilder/ui-ux-pro-max-skill` |
| Upstream commit | `8bd29e775453ebcae52b6e6514fbf134df0c5770` |
| Imported | 2026-08-28 |
| Upstream license | MIT |
| Included license | `THIRD_PARTY_LICENSE.txt` |

## Included

- `.claude/skills/ui-ux-pro-max/SKILL.md`
- the skill's `data/` catalogue
- the skill's `references/`
- runtime Python files: `core.py`, `design_system.py`, `reasoning_contract.py`,
  `search.py`, and `validate_data.py`

## Deliberately omitted

- upstream repository documentation unrelated to runtime use
- upstream tests and test fixtures
- catalogue refresh, evaluation and release-maintenance utilities
- editor integrations and packaging metadata

## Local adaptation

The behavioural-path adaptation replaces the upstream standalone skill path
`${CLAUDE_PLUGIN_ROOT}/.claude/skills/ui-ux-pro-max/` with this marketplace plugin's
path `${CLAUDE_PLUGIN_ROOT}/skills/ui-ux-pro-max/`. Three anti-pattern examples use
`<user>`/`<app>` placeholders instead of account-like absolute paths so the toolkit
does not ship personal-looking filesystem strings. Catalogue meaning and runtime
Python logic are otherwise copied from the pinned commit. The generated
`phosphor-icons-upstream.json` catalogue is JSON-minified without changing its parsed
data, keeping the vendored plugin smaller and avoiding transport-dependent formatting;
the corresponding raw-file fingerprint in `catalog-summary.json` is updated.

The imported runtime was audited for network and subprocess access and exercised
against its upstream test suite before adoption. The toolkit validation pins this
commit, checks the included license and runtime file set, scans runtime imports and
dangerous APIs, and performs a local JSON design-system search.
