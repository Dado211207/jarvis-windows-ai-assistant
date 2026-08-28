---
name: safe-edit
description: Edit files without collateral damage - preserve unrelated user changes, keep diffs minimal and reviewable, and never rewrite generated or protected files by hand. Use before and during any editing session, especially when the working tree is already dirty.
---

# Safe editing

The user's uncommitted work is not yours. Neither is the formatting of files you
were not asked to touch.

## 1. Before the first edit

```
git status --porcelain=v1
git stash list
```

- If files are already modified, **write down which ones.** Those are user changes.
- Never run `git stash`, `git checkout -- <file>`, `git restore`, `git reset` or
  `git clean` to "get a clean tree" unless the user asked for exactly that.
- Never revert or re-format a file that is dirty for reasons unrelated to your task.

## 2. Read before you write

Read the region you are about to change, plus enough surrounding code to match its
conventions. Write code that reads like the code around it: same naming, same error
handling, same comment density. A change that is stylistically foreign is harder to
review even when it is correct.

## 3. Keep the diff to the change

- Prefer targeted edits over rewriting a whole file.
- No drive-by reformatting, import reordering, or "while I'm here" refactors.
- No trailing-whitespace or line-ending churn — if your editor wants to normalise
  the whole file, undo it.
- One concern per commit. If you find a second bug, note it; do not fold it in.

## 4. Files you do not hand-edit

| Kind | Correct action |
| --- | --- |
| Lockfiles (`package-lock.json`, `poetry.lock`, …) | Regenerate with the project's package manager |
| Generated code, snapshots, compiled output | Regenerate with the project's tool |
| `.env*`, credential files, key material | Never read, never write, never print |
| Vendored third-party directories | Change upstream or document a patch, do not edit in place |
| CI workflow permissions, branch protection | Only when explicitly asked |

## 5. Re-check after editing

```
git status --porcelain=v1
git diff --stat
git diff
```

Read your own diff as a reviewer would. Ask: is every hunk here on purpose? If a
file appears that you did not intend to touch, revert **that file only** and say so.

## 6. Encoding and line endings

Match the file you are editing. Do not introduce a BOM. Do not convert LF to CRLF
or back. If the repository has a `.gitattributes`, it is authoritative.

## 7. When an edit fails

If a targeted edit will not apply cleanly, do not fall back to overwriting the whole
file from memory — you will silently drop the user's concurrent changes. Re-read the
file and redo the targeted edit.
