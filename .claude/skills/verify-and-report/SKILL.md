---
name: verify-and-report
description: Run the final verification pass and write a report that cannot mislead - exact commands with exit codes, exact git identity, and an explicit list of what was skipped, blocked or left unverified. Use before handing work back, before saying a task is done, and before opening or updating a pull request.
---

# Final verification and reporting

Read `${CLAUDE_PLUGIN_ROOT}/reference/honesty-rules.md` if you have not this session.
The single rule that matters: **a check that did not run did not pass.**

## 1. Verify git identity, with output

Do not describe git state from memory. Print it:

```
git status --porcelain=v1          # expect empty, or exactly the files you intended
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git rev-parse @{u} 2>/dev/null     # remote head, if the branch is tracked
git log --oneline -5
```

Confirm and report:

- local HEAD == remote branch head (if pushed) — compare the two SHAs
- the branch is the one you were told to use, and is **not** the default branch
- no file changed that you did not intend
- if a pull request exists: its head SHA equals the SHA you are reporting

## 2. Re-run the checks, do not recall them

Run the selected checks once more against the final tree and paste the results:

```
<command>  -> exit <code>   (<n> passed, <n> failed, <n> skipped)
```

For anything that matters, run it twice and report both totals. Two identical runs
is evidence; one run plus a memory is not.

## 3. Self-review the diff

```
git diff <base>...HEAD --stat
git diff <base>...HEAD
```

Read it adversarially. Look for: debug prints, commented-out code, TODOs you left,
secrets or tokens, absolute local paths, unrelated files, generated files edited by
hand, and anything that widens permissions.

## 4. Scan for what must never ship

- credentials, API keys, tokens, cookies, private keys, connection strings
- absolute paths containing a user name (`/home/<user>/…`, `C:\Users\<user>\…`)
- personal data that is not part of the product
- an `allow`-everything permission rule, a disabled safety check, a weakened CI gate

## 5. Write the report

```
Done:        <what actually works now, one line each>
Not done:    <what was in scope and is not finished, and why>
Commands:    <command> -> exit <code>  (<counts>)      [one line per check]
Not run:     <check> — <reason it could not run, and what that leaves unverified>
Git:         branch=<…> head=<…> remote=<…> tree=<clean|…>
Changed:     <file list>
Assumptions: <anything you assumed rather than verified>
Risks:       <what could still be wrong>
Needs you:   <manual steps only the user can do>
```

## 6. Things that are never in the report

- "should work", "looks good", "everything passes" without the commands
- a check described as passing when it was skipped, cancelled or never run
- a claim about hardware, real devices, or human perception from a headless session
- a claim that a repository, branch or PR is in a state you did not print

## 7. Stop line

Finishing verification is not permission to merge, tag, release or deploy. Report,
then wait for a task-specific instruction from the user.
