---
name: select-tests
description: Choose which checks to run for a change and what each one actually proves, using the project's own commands. Use before claiming a change is verified, when deciding whether a full suite is needed, or when a project defines many overlapping check commands.
---

# Test selection

Running everything is slow; running nothing is dishonest. Pick the smallest set of
checks that covers the blast radius of the change, and state what each one proves.

## 1. Use the project's real commands

Read them out of the project, never from habit:

- `package.json` → `scripts`
- `pyproject.toml`, `tox.ini`, `noxfile.py`, `Makefile`
- `.github/workflows/*` → what CI actually enforces (this is the authoritative list)
- `playwright.config.*`, `vitest.config.*`, `pytest.ini`, `jest.config.*`

If the project defines no test command, say so. Do not invent `npm test`.

## 2. Match checks to the blast radius

| What changed | Minimum set |
| --- | --- |
| One pure function | Its unit tests + typecheck |
| A component or module boundary | Unit tests of both sides + typecheck + lint |
| A route, page or public API | Integration/e2e for that route + the unit set |
| Build config, bundler, tsconfig | Full build, then the unit set |
| Dependencies or lockfile | Full build + full suite; a partial run proves nothing |
| Packaging, installer, CI config | The packaging job itself; unit tests do not cover it |
| Copy, translations, metadata | Content checks + a render of the affected pages |

When in doubt, widen. A missed regression costs more than a slow run.

## 3. Know what each check does not prove

- **Typecheck** proves types, not behaviour.
- **Lint** proves style and a few bug classes, nothing about correctness.
- **Unit tests** prove units in isolation, not integration or rendering.
- **A green build** proves it compiles, not that it works.
- **Headless browser tests** prove DOM and script behaviour, not visual design,
  not real fonts on a real machine, not performance on real hardware.
- **No automated check** proves audio, microphone, camera, GPU, antivirus, installer
  UX, or how a page feels. Those are `requires-manual-acceptance`.

## 4. Run and record

Record every command with its exit status and counts:

```
<command>  -> exit <code>   (<n> passed, <n> failed, <n> skipped)
```

Skips are not passes. If a suite reports skipped tests, say how many and why —
a suite that silently skips the tests covering your change is a false green.

## 5. Compare against the baseline

If a check was already failing before your change, say so explicitly:

```
<command> -> exit 1  (2 failed)  — both pre-existing on <base SHA>, unrelated to this change
```

Never present a pre-existing failure as caused by your change, and never present
your own new failure as pre-existing. Verify by running the same command on the
base commit when the distinction matters.

## 6. When a check cannot run

State it in the report as `not-run` with the reason (missing binary, no network,
no display, unsupported OS, sandbox restriction). Then say what that leaves
unverified. Do not substitute a weaker check and describe it as the stronger one.
