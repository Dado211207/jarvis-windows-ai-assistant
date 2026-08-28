---
name: pre-change-snapshot
description: Capture the exact repository state before a change so later claims about base, head and approval can be checked. Use at the start of any release-sensitive task, before opening a pull request, and before comparing an approved commit against what is actually deployed.
---

# Pre-change snapshot

Every later claim — "this is what was approved", "main is unchanged", "the PR head
matches" — is only checkable against a snapshot you took first. Take it before the
first edit.

## Capture

```
git remote -v
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git rev-parse origin/<default-branch>
git status --porcelain=v1
git stash list
git log --oneline -10
```

Record, verbatim:

```
Origin:        <url>
Branch:        <name>          (must not be the default branch)
Base SHA:      <full 40-char SHA of the branch you will branch from>
Head SHA:      <full 40-char SHA now>
Tree:          clean | <exact list of dirty files>
Stashes:       <count>
```

## Clean-tree check

If the tree is dirty, those changes are the user's. Do not stash, reset, restore or
clean them. List them, state that you are leaving them alone, and confirm none of
them is a file your task will also touch. If one is, stop and ask.

## Branch check

Confirm you are **not** on the default branch. If you are, create the feature branch
now, before editing:

```
git switch -c <branch-name>
```

Never commit to the default branch, and never rewrite history on a branch someone
else may have checked out.

## Base check

If you were given an expected base SHA, compare it to what you measured and report
any difference before doing anything else. A base that moved means the approval you
are working from may not apply.

## Re-verify at the end

At hand-back, print the same set again and show the delta:

```
Base SHA:   <unchanged from snapshot>
Head SHA:   <old> -> <new>
Commits:    <the exact list added>
Default branch: <SHA> — unchanged since snapshot: yes | no
Tree:       clean | <files>
```

If the default branch moved during your work, say so. It changes what your diff
means and may create a merge conflict that is now yours to resolve.
