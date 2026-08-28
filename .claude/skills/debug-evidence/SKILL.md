---
name: debug-evidence
description: Debug from evidence instead of guesses - reproduce first, isolate the cause, then fix the cause rather than the symptom. Use when something fails, when a test is red, when behaviour differs between environments, or when a previous fix did not hold.
---

# Evidence-first debugging

The rule: **reproduce, then explain, then fix.** A fix applied before you can
explain the failure is a guess, and a guess that makes red go green is worse than
red because it hides the real defect.

## 1. Reproduce and record

Get the failure to happen on demand and capture it verbatim:

```
<command>  -> exit <code>
<the actual error text, not a paraphrase>
```

If you cannot reproduce it, say so and stop. "Could not reproduce" is a legitimate,
useful result. Fixing an unreproduced failure is not.

Note whether the failure is deterministic. Run it three times if the answer matters.

## 2. Read the whole error

Before forming a hypothesis:

- read the full stack trace, bottom frame *and* top frame
- check the line the error actually names, not the line you expected
- check whether the error is the first error or a cascade from an earlier one
- look for the last thing that worked, not the first thing that broke

## 3. Isolate

Narrow the surface until the failure is a single cause:

- bisect the input (smaller fixture, single test, single route)
- bisect the code (`git log -S<symbol>`, `git bisect` when the regression window is known)
- bisect the environment (does it fail in CI only? on one OS only? with a cold cache only?)

State what you eliminated and how. "Not a caching issue" is only worth writing if
you can say what you did to rule it out.

## 4. Classify before fixing

| Class | Fix belongs in |
| --- | --- |
| `product-defect` | The source under test |
| `test-defect` | The test, fixture or assertion |
| `environment` | Setup, tooling, CI config — and it is reported, not silently patched |
| `flaky` | The source of the non-determinism (timing, ordering, shared state) |
| `not-run` | Nothing yet — you have no evidence at all |

A `flaky` classification needs both outcomes observed. Otherwise it is unexplained.

## 5. Fix the cause

- Change the smallest thing that removes the cause.
- Do not raise a timeout, add a retry, relax an assertion, or skip a case to get green.
  If timing is genuinely the cause, fix what is racing — then, if a wait is still
  needed, wait on the condition, not on the clock, and say why.
- Do not bundle unrelated cleanups into a bug fix. They hide the diff that matters.

## 6. Prove it, both directions

A fix is verified when you have shown both:

1. The failing case now passes — same command, same input, exit 0.
2. The failure returns if you revert the fix — or you can explain precisely why the
   change is causal without reverting.

Then re-run the surrounding suite: a fix that breaks a neighbour is not a fix.

## 7. Report

```
Symptom:    <observed, verbatim>
Reproduce:  <command> -> exit <code>
Cause:      <the actual mechanism>
Class:      product-defect | test-defect | environment | flaky
Fix:        <files changed and why this removes the cause>
Proof:      <command> -> exit 0     (before: exit <code>)
Not fixed:  <anything still failing, still unexplained, or out of scope>
```
