# Work log — PR #17, v0.2 installer repair

A public, tracked handoff record. Not authorization, and not cross-session memory.
Code and current owner instructions take precedence if this becomes stale. No
secrets, personal data, absolute local paths, usernames or signed artifact URLs.

- Branch: `claude/fix-v02-inno-installer`
- Pull request: [#17](https://github.com/Dado211207/jarvis-windows-ai-assistant/pull/17) — Draft, open, unmerged
- Base: `main` at `a6eaeac282d4e873c6a3487b20ae4c8b42d82db9` (unchanged throughout)

## Why this branch exists

`Windows Installer` run `33185176244`, the post-merge run for PR #15 on
`main@a6eaeac`, failed. Three separate defects were found, each hidden behind
the one before it — fixing one exposed the next.

## Defects and corrections

| # | Commit | Defect | Evidence it is fixed |
| --- | --- | --- | --- |
| 1 | `b890df5` | `packaging/jarvis.iss` line 415 began with `#13#10`; ISPP reads a line-leading `#` as a directive, so it parsed `13` as the directive name → `Unknown preprocessor directive`, ISCC exit 2 | `Successful compile (66.828 s)`, installer produced |
| 2 | `85044f7` | `scripts/installed_coding_acceptance.py` confirmed the preview by `project_id`; `/coding/preview/start` requires a reviewed `plan_id` → HTTP 422 | `OK: preview plan … describes ['npm','run','dev']` |
| 3 | `2f61b34` | The installer path gate omitted `scripts/installed_coding_acceptance.py`, so a change to the acceptance logic **skipped** the only job that runs it while reporting success | detector required the build; installer job ran |
| 4 | pending | The same gate omitted `.github/workflows/windows-build.yml`; run `33272737324` skipped the installer job (`99154063483`) after a push touching only that file | regression test below |

Defects 2 and 3 were pre-existing on `main` and byte-identical there — they were
unreachable because the ISPP error aborted every installer run about forty
minutes before those stages.

Defect 2 was corrected in the acceptance script, **not** by widening the endpoint
to accept `project_id`. Confirming by project id would run whatever the script
says now rather than what was reviewed, which is the check that flow exists for.

## Regression tests, each proven in both directions

| Test | Fails against | Passes against |
| --- | --- | --- |
| `test_no_line_starts_with_a_pascal_character_code_expression` | pre-fix `.iss`, naming line 415 | fixed `.iss` |
| `test_the_acceptance_phase_posts_bodies_the_coding_api_actually_accepts` | pre-fix script — `posts ['project_id'] … requires ['plan_id']` | fixed script |
| `test_the_gate_watches_every_script_the_acceptance_phase_runs` | pre-fix workflow | fixed workflow |
| `test_the_gate_watches_the_workflows_that_gate_windows_verification` | workflow with only `windows-build.yml` coverage removed | fixed workflow |

## Workflow evidence

| Purpose | Run | Head | attempt | Result |
| --- | --- | --- | ---: | --- |
| CI | `33249220818` | `2f61b34` | 1 | success |
| Windows Build | `33249220821` | `2f61b34` | 1 | success |
| Windows Installer (PR) | `33249220836` | `2f61b34` | 1 | success — `ALL CLEAN-INSTALL CHECKS PASSED` |
| Installer acceptance 1 | `33250047123` | `2f61b34` | 1 | success — `ALL CLEAN-INSTALL CHECKS PASSED` |
| Installer acceptance 2 | `33251235787` | `2f61b34` | 1 | **failure** — see below |
| Windows Installer | `33272737324` | `2c6bbef` | 1 | detector success, installer job **skipped** — not a pass |
| Survivor diagnostics (12×) | `33272738623` | `2c6bbef` | 1 | success, 0 survivors |
| Survivor diagnostics (30×) | `33297310832` | `2c6bbef` | 1 | success, 0 survivors |

Two installer builds succeeded end to end on `2f61b34`, on independent runners,
each producing `JARVIS-Setup-v0.2.0-rc1-x64.exe` and reaching
`ALL CLEAN-INSTALL CHECKS PASSED`. The installer is not byte-reproducible: size
and SHA-256 differ per build, as expected from embedded timestamps and
compression nondeterminism.

## Open question — the `msedge.exe` survivor

Acceptance run `33251235787` failed inside the build's pytest, before Inno Setup:

```
test_no_browser_process_survives_any_fixture[alias.html]
  14 leftover process(es) in 6.05s: already_gone=1, still_alive=1, terminated=12
test_no_browser_process_survives_any_fixture[clean.html]
  11 leftover process(es) in 6.06s: already_gone=1, killed=1, still_alive=1, terminated=8
```

One `msedge.exe` remained after both `terminate()` and `kill()`. On Windows
psutil's `terminate()` **is** `kill()` — both call `TerminateProcess` — so the
second stage adds no new force, only a second observation window. Both 3 s graces
were fully consumed, which is what the ~6.05 s duration shows.

`tests/test_coding_browser_qa.py` is byte-identical to `main`; nothing in this
branch touches browser QA or process teardown.

### What was unrecorded, and the scaffolding added for it

`27ca75b` and `2c6bbef` added temporary diagnostics: `CleanupResult` gained
`source`, `terminate_error`, `kill_error`, `wait_error`, `final_state` and
`final_checked`, and after the kill grace every remaining target is re-resolved
by PID **and** creation time. Pass/fail is deliberately unchanged —
`CleanupReport.survivors` and the guard test behave exactly as before.

This matters because `still_alive` previously meant only *"`wait_procs` did not
observe an exit within the shared budget"*, not *"this PID and creation time are
still present"*. Those are different claims needing different corrections.

### Diagnostic result — not reproduced in 42 iterations

| Run | Iterations | Cleanup passes | Processes | Survivors | Artifact |
| --- | ---: | ---: | ---: | ---: | --- |
| `33272738623` | 12 | 36 | 407 | **0** | `9720737409` |
| `33297310832` | 30 | 90 | 1133 | **0** | `9727981753` |
| **Total** | **42** | **126** | **1540** | **0** | |

Across both runs, on `2c6bbeff3db4caf2ba0cf8bbeafae5ec8779def3`:

- outcomes were only `terminated` and `already_gone` — no `killed`,
  `still_alive`, `pid_reused` or `inaccessible`;
- no `terminate_error`, `kill_error` or `wait_error` was recorded;
- no `final_state` was recorded, because nothing survived to the final check;
- **`kill_sent` was false for all 1540 processes** — the terminate stage alone
  settled every one;
- every pass completed in ≤ 0.610 s.

That is the sharpest contrast with the failure, which consumed the full 6 s and
did send `kill()`. The failing regime is qualitatively different from every
healthy one observed, not a marginal timing miss — but *why* it differs is still
unknown.

**Decision: the root-cause correction is stopped.** The failure remains
unreproduced and the cause unproven. Per the standing instruction, do not
implement the candidate correction, widen timeouts, weaken `survivors == []`,
add retries, or perform a name-based sweep on this evidence. The defect is not
resolved; it is unreproduced.

The scaffolding is deliberately left in place, because it is the only thing that
would answer the question if the survivor reappears.

### If a survivor is reproduced

Classify its final PID + creation-time re-resolution as exactly one of:

- `already_gone` → the observation path is the cause; the narrow correction is
  the final identity-aware verification before declaring a survivor;
- `still_alive` with the same identity → Edge genuinely survived
  `TerminateProcess`; stop, because that needs a different design;
- `pid_reused` or `inaccessible` → stop and report; do not apply the
  `already_gone` correction.

## Known gaps

- The diagnostics JSONL records no route or iteration label, so route attribution
  is inferred from the repeating record structure and corroborated by the harness
  log (`routes: alias.html, clean.html`), not read directly from the record.
- The 14 Windows-only skips in the installer build's suite are not enumerated —
  that build runs `pytest` without `-rs`.
- `main` has no branch protection. Noted, deliberately unchanged.

## Scaffolding to remove

`scripts/diagnose_survivor_flake.py`, the `survivor_iterations` input and its two
steps in `.github/workflows/windows-build.yml`, and the diagnostic fields in
`app/launcher/process_tree.py` are temporary. Remove them once the survivor
question is answered and a deterministic regression replaces them, unless a
specific permanent diagnostic is justified.

## Next action

1. Get a **real, non-skipped** Windows Installer run green on the final head —
   the defect-4 correction makes this branch's own workflow changes trigger it.
2. Then two sequential Windows Installer `workflow_dispatch` acceptance runs on
   that head, not concurrent, neither re-run on failure.
3. Leave the survivor question open. If it reappears, the scaffolding will record
   the final PID + creation-time re-resolution that decides it.

Do not merge, tag, release, deploy, or ask the owner to install JARVIS.
