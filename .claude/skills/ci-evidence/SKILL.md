---
name: ci-evidence
description: Establish what CI actually proved by reading job results and logs, separating required checks from informational ones and counting skips, cancellations and retries. Use before saying CI is green, before reporting a pull request ready, and whenever a check result is quoted as evidence.
---

# CI evidence

A green tick is a UI element. Evidence is the job's conclusion plus what its log
shows it actually ran.

## 1. Bind the result to a commit

A check result belongs to one commit. Always record:

```
Checks read for SHA: <full 40-char SHA>
PR head SHA:         <full 40-char SHA>
Match:               yes | NO — the checks describe a different commit
```

If a push happened after the checks ran, the checks do not describe the current
head. Say so rather than quoting the stale result.

## 2. Enumerate every check, not just the failures

For each check on that SHA:

```
<check name>   <conclusion>   <required|informational>   <duration>
```

Conclusions that are **not** success: `failure`, `cancelled`, `timed_out`,
`action_required`, `stale`, `skipped`, `neutral`, and "never started". Only
`success` is success.

- **Required vs informational**: read the repository's branch-protection rules or the
  merge box. An informational check that is red is still a real signal; it just does
  not block. Report both, and label which is which.
- **Skipped**: a job skipped by a path filter or an `if:` condition proves nothing
  about your change. If the job that covers your change skipped, that is a coverage
  gap, not a pass.
- **Cancelled**: usually a superseding push or a timeout. Never report as pass.

## 3. Read the log, not just the conclusion

A job can exit 0 and still have run nothing. Open the log and confirm:

- the test step actually executed, and how many tests ran
- the counts: passed / failed / skipped. A suite that ran 0 tests is a red flag
- no step was silently swallowed by `|| true`, `continue-on-error`, or a shell that
  does not stop on error
- the matrix leg you care about ran (the OS, the Node/Python version, the browser)

Record the counts, not just "green".

## 4. Retries and reruns

If a job was re-run, say so and say why:

```
<check>  attempt 1: failure  attempt 2: success   — rerun reason: <what changed>
```

A job that only passes on a re-run of the same commit is unstable. Do not present
the second attempt as the result and drop the first. If nothing changed between
attempts, you have evidence of non-determinism, and that is a finding.

## 5. Compare against the base branch

Before attributing a failure to the change under review, check the same job on the
base branch. A check that is red on the base too is not this change's defect — but
it is still not a pass, and saying nothing about it is hiding a failure.

## 6. Report

```
SHA:            <full SHA>
Required:       <n> success, <n> failure, <n> skipped, <n> cancelled
Informational:  <n> success, <n> failure, <n> skipped
Test counts:    <job> — <n> passed, <n> failed, <n> skipped
Reruns:         <none | list with attempt outcomes>
Base branch:    <same jobs on base — green | red (which)>
Not verified:   <what the logs could not tell you>
Conclusion:     CI proves <what exactly>. It does not prove <what>.
```

Never write "CI is green" without the SHA and the counts behind it.
