---
name: uncertainty-log
description: Track what is fact, what is inference and what is unverified assumption throughout a task, so the final report can separate them. Use when working from incomplete information, when the user asks how confident you are, or when a claim would be expensive to get wrong.
---

# Uncertainty tracking

Most bad agent output is not a lie; it is an inference that lost its label on the
way to the report. Keep the labels attached.

## The three labels

| Label | Test | How to write it |
| --- | --- | --- |
| **Fact** | A file says it, or a command printed it | Cite `path:line` or the command |
| **Inference** | You concluded it from facts | Say what it rests on |
| **Assumption** | You do not know and did not check | Say so, and say what would settle it |

A fourth category matters just as much: **contradiction** — two sources disagree.
Never silently pick one. Report both and say which you acted on and why.

## Rules

1. **Do not promote.** An inference does not become a fact by being repeated, by
   being probably right, or by being convenient for the report.

2. **Cite or downgrade.** If you cannot produce the path or command behind a claim,
   it is an inference at best. Common false facts: "the project uses X" (from the
   repository name), "the tests cover Y" (from a filename), "this is the production
   URL" (from a README that may be stale).

3. **Recency.** A README, a comment or a state file describes the past. The code and
   the command output describe now. When they conflict, prefer what runs.

4. **Absence is not evidence.** "I found no tests for this" means you did not find
   them. Say where you looked.

5. **Cost-weighted checking.** Verify in proportion to blast radius. A wrong guess
   about an indentation style costs nothing; a wrong guess about which branch
   deploys to production costs a lot. Check the second one.

## When to stop and ask

Ask the user when **all three** hold: the answer changes what you do, you cannot
determine it from the repository, and proceeding on the wrong branch would waste or
damage work. Otherwise pick the conservative reading, state the assumption, and keep
going — do everything that does not depend on the answer first.

Never invent a value to unblock yourself. If a name, date, credential, URL, version
or measurement is unknown, leave a marked placeholder and list it under "Needs you".

## Carrying it into the report

The final report has an `Assumptions:` line. Fill it from this log. If it is empty
because you verified everything, say that explicitly — an empty assumptions list
should be a claim you are making, not a section you forgot.
