---
name: plan-change
description: Turn a vague or multi-part request into an explicit, verifiable plan with scope boundaries and a stop line. Use before starting a change that touches more than one file, has unclear requirements, or could affect release, deployment or user data.
---

# Plan a change

A plan is useful only if it is falsifiable. Every step must name what you will do
and how you will know it worked.

## 1. Restate the request as scope

Write three lists. Be blunt; the value is in the third one.

```
In scope:      <what the user asked for, restated>
Out of scope:  <adjacent things you will NOT do>
Unknown:       <what you would need the user to confirm>
```

If two readings of the request lead to materially different work, ask **one**
question now rather than building the wrong thing. If both readings lead to
similar work, pick the more conservative one and say which you picked.

## 2. Establish the baseline

You cannot verify a change without a before. Record, before editing:

- current branch and full HEAD SHA
- current state of the behaviour you are about to change (test output, a screenshot, a log line, the current rendered string)
- which checks are green *right now* — a failure that already existed is not yours to hide, and not yours to silently inherit

## 3. Decompose into verifiable steps

For each step:

```
Step N: <action>
  Touches:  <exact files>
  Verify:   <the exact command or observation that proves it>
  Rollback: <how to undo just this step>
```

A step whose `Verify` line is "looks right" is not a step. Replace it with a
command, a diff, or an explicit `requires-manual-acceptance` marker.

## 4. Name the stop line

State, in the plan, where you will stop and hand back. Default stop lines:

- before `git merge`, a release, a tag, or any deployment
- before changing repository settings, branch protection, or visibility
- before touching a repository the user did not name
- before adding a runtime dependency

General permission to build or fix is **not** permission to merge or deploy.

## 5. Size the risk

One line each:

- **Blast radius** — what breaks if this is wrong?
- **Reversibility** — can it be reverted with a commit, or is it a one-way door?
- **Detection** — how would anyone notice it was wrong?

A one-way door with no detection needs the user's confirmation before you start,
not after.

## 6. Keep the plan alive

When reality contradicts the plan, update the plan and say so. Silently deviating
turns the final report into fiction. If the change grows past the scope you wrote,
stop and re-scope rather than widening it on your own.
