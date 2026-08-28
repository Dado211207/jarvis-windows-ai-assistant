"""Git, with the destructive half removed.

The premise of this module is that **the user's uncommitted work is not
JARVIS's to touch**. Everything else follows from that.

A project is very often mid-thought: a half-finished refactor, a debug
print, three untracked scratch files. A coding agent that runs
`git checkout .` or `git stash` to "get a clean state" has destroyed work
somebody was in the middle of, and no amount of apology in the summary
puts it back. So:

* Reading is free. `status`, `diff`, `log`, `branch` and friends run
  without asking.
* **Nothing that can discard work exists here at all.** There is no
  function in this module that runs `reset`, `checkout`, `clean`,
  `stash`, `rebase`, or a force anything. Not gated — absent. See
  `app/coding/commands.py::GIT_BLOCKED`, and the test that greps this
  module's own source for those verbs.
* Isolation is preferred to negotiation: when the repository is in a
  state where a worktree can be created safely, the task works in a
  separate worktree on its own branch, and the user's working copy is
  never touched at all.
* When isolation is *not* safe, the task stops and explains. It does not
  proceed in the user's working copy "carefully".

**Credentials in remote URLs.** A remote can be
`https://user:token@github.com/...`. That token must never reach a task
record, a log, an event or a model. `remotes()` strips userinfo before
returning anything.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from app.coding.runner import build_environment
from app.coding.workspace import is_protected
from app.logging_config import get_logger

logger = get_logger("coding.gitsafe")

# Verbs this module must never contain. Asserted by a test that reads this
# file, so adding one is a build failure rather than a review question.
FORBIDDEN_VERBS = (
    "reset", "checkout", "clean", "stash", "rebase", "push",
    "filter-branch", "reflog", "prune",
)

_CREDENTIAL_IN_URL = re.compile(r"://[^/@\s]+@")


@dataclass
class GitStatus:
    is_repository: bool
    root: Optional[str] = None
    branch: Optional[str] = None
    detached: bool = False
    staged: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    untracked: List[str] = field(default_factory=list)
    conflicted: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def is_dirty(self) -> bool:
        return bool(self.staged or self.modified or self.untracked or self.conflicted)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicted)

    def as_dict(self) -> dict:
        return {
            "is_repository": self.is_repository,
            "root": self.root,
            "branch": self.branch,
            "detached": self.detached,
            "staged": self.staged,
            "modified": self.modified,
            "untracked": self.untracked,
            "conflicted": self.conflicted,
            "is_dirty": self.is_dirty,
            "has_conflicts": self.has_conflicts,
            "error": self.error,
        }


def _git(root: Path, args: List[str], timeout: float = 20.0) -> Tuple[int, str, str]:
    """Run a git command with argv, no shell, a minimal environment and a
    bounded wait. Returns (exit_code, stdout, stderr).

    `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS` matter: without them a git
    command that wants credentials blocks forever on a prompt nobody can
    see, which in a desktop app looks exactly like a hang.
    """
    env = build_environment()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GCM_INTERACTIVE"] = "never"
    try:
        completed = subprocess.run(  # noqa: S603 — argv list, shell=False
            ["git", *args],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        return completed.returncode, completed.stdout, completed.stderr
    except FileNotFoundError:
        return 127, "", "Git is not installed, or is not on PATH."
    except subprocess.TimeoutExpired:
        return 124, "", f"The git command did not finish within {timeout:.0f}s."
    except OSError as exc:
        return 1, "", f"The git command could not run ({type(exc).__name__})."


def strip_credentials(url: str) -> str:
    """`https://user:token@host/repo` -> `https://host/repo`."""
    return _CREDENTIAL_IN_URL.sub("://", url or "")


def is_repository(root: Path) -> bool:
    code, out, _ = _git(root, ["rev-parse", "--is-inside-work-tree"])
    return code == 0 and out.strip() == "true"


def status(root: Path) -> GitStatus:
    """The repository's current state, including whose changes are already
    there before JARVIS does anything."""
    if not is_repository(root):
        return GitStatus(is_repository=False)

    result = GitStatus(is_repository=True)

    code, out, _ = _git(root, ["rev-parse", "--show-toplevel"])
    if code == 0:
        result.root = out.strip()

    code, out, _ = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if code == 0:
        branch = out.strip()
        if branch == "HEAD":
            result.detached = True
            code2, out2, _ = _git(root, ["rev-parse", "--short", "HEAD"])
            result.branch = f"detached at {out2.strip()}" if code2 == 0 else "detached"
        else:
            result.branch = branch

    # -z gives NUL-separated entries, which is the only form that survives
    # filenames containing spaces, quotes or newlines.
    code, out, err = _git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if code != 0:
        result.error = err.strip()[:200] or "git status failed"
        return result

    entries = [e for e in out.split("\0") if e]
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 3:
            continue
        code_x, code_y, path = entry[0], entry[1], entry[3:]
        if code_x in ("R", "C"):
            # A rename's second path follows as its own NUL-separated field.
            index += 1
        if code_x == "U" or code_y == "U" or (code_x == "A" and code_y == "A") or (code_x == "D" and code_y == "D"):
            result.conflicted.append(path)
        elif code_x == "?" and code_y == "?":
            result.untracked.append(path)
        else:
            if code_x not in (" ", "?"):
                result.staged.append(path)
            if code_y not in (" ", "?"):
                result.modified.append(path)

    return result


def diff(root: Path, staged: bool = False, max_bytes: int = 200_000) -> str:
    """The working-tree diff, with protected files excluded.

    **This function is a way for a file's contents to leave the project,
    and `git diff` knows nothing about the protected-path engine.** A
    plain `git diff` over a repository whose `.env` has been edited puts
    the key in the diff — and this diff is served over HTTP, rendered in
    the Changes tab, and available to a task record.

    That is not hypothetical: the Windows CI job caught it. Every file
    showed as modified there because of line-ending normalisation, and
    `GET /coding/projects/{id}/diff` returned the fixture's fake
    Anthropic key, its Stripe secret, its npm token and its private key.
    On Linux the same test passed only because nothing happened to be
    modified.

    So the changed paths are listed first, the protected ones are removed
    by the same `is_protected()` every other read goes through, and git
    is asked for a diff of what remains. An excluded file is *named* —
    the user should know their `.env` differs from HEAD; they just should
    not be shown what it now says.
    """
    args = ["diff", "--no-color"]
    if staged:
        args.append("--staged")

    # Which files would this diff cover?
    name_args = list(args) + ["--name-only"]
    code, listing, _ = _git(root, name_args, timeout=30.0)
    if code != 0:
        return ""

    changed = [line.strip() for line in listing.splitlines() if line.strip()]
    permitted, withheld = [], []
    for path in changed:
        (withheld if is_protected(PurePosixPath(path)) is not None else permitted).append(path)

    header = ""
    if withheld:
        header = (
            "[" + str(len(withheld)) + " protected file(s) changed and are not shown: "
            + ", ".join(sorted(withheld)[:20])
            + ". JARVIS never displays the contents of a file that holds credentials.]\n\n"
        )

    if not permitted:
        return header

    # `--` separates paths from revisions, so a file named like a branch
    # cannot be reinterpreted as one.
    code, out, _ = _git(root, args + ["--"] + permitted, timeout=30.0)
    if code != 0:
        return header

    body = header + out
    if len(body.encode("utf-8", errors="replace")) > max_bytes:
        return body[:max_bytes] + f"\n[diff truncated at {max_bytes:,} bytes]"
    return body


def log(root: Path, count: int = 10) -> List[dict]:
    code, out, _ = _git(
        root, ["log", f"-{max(1, min(count, 50))}", "--no-color", "--pretty=format:%h%x1f%an%x1f%ar%x1f%s"]
    )
    if code != 0:
        return []
    entries = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            entries.append({"hash": parts[0], "author": parts[1], "when": parts[2], "subject": parts[3]})
    return entries


def remotes(root: Path) -> List[dict]:
    """Remotes, with any embedded credentials removed."""
    code, out, _ = _git(root, ["remote", "-v"])
    if code != 0:
        return []
    seen: Dict[str, dict] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            seen[parts[0]] = {"name": parts[0], "url": strip_credentials(parts[1])}
    return list(seen.values())


def worktrees(root: Path) -> List[dict]:
    code, out, _ = _git(root, ["worktree", "list", "--porcelain"])
    if code != 0:
        return []
    entries: List[dict] = []
    current: Dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            if current:
                entries.append(dict(current))
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(dict(current))
    return entries


# --------------------------------------------------------------------------
# Pre-existing user changes
# --------------------------------------------------------------------------

@dataclass
class UserChangeSnapshot:
    """What was already changed before this task started.

    The point of recording this is so the UI can say "these 4 files were
    already modified when the task began" and never attribute them to
    JARVIS. Being able to tell the two apart is the difference between a
    diff a user can trust and one they cannot.
    """

    taken: bool
    staged: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    untracked: List[str] = field(default_factory=list)
    conflicted: List[str] = field(default_factory=list)
    head: Optional[str] = None
    branch: Optional[str] = None

    @property
    def all_paths(self) -> List[str]:
        return sorted(set(self.staged + self.modified + self.untracked + self.conflicted))

    def as_dict(self) -> dict:
        return {
            "taken": self.taken,
            "staged": self.staged,
            "modified": self.modified,
            "untracked": self.untracked,
            "conflicted": self.conflicted,
            "head": self.head,
            "branch": self.branch,
            "count": len(self.all_paths),
        }


def snapshot_user_changes(root: Path) -> UserChangeSnapshot:
    state = status(root)
    if not state.is_repository:
        return UserChangeSnapshot(taken=False)
    code, out, _ = _git(root, ["rev-parse", "HEAD"])
    return UserChangeSnapshot(
        taken=True,
        staged=list(state.staged),
        modified=list(state.modified),
        untracked=list(state.untracked),
        conflicted=list(state.conflicted),
        head=out.strip() if code == 0 else None,
        branch=state.branch,
    )


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------

@dataclass
class IsolationPlan:
    """Whether the task can be isolated, and if not, exactly why."""

    possible: bool
    reason: str
    strategy: str                    # "worktree" | "in_place" | "none"
    branch_name: Optional[str] = None
    worktree_path: Optional[str] = None
    start_sha: Optional[str] = None
    blockers: List[str] = field(default_factory=list)
    creation_attempted: bool = False
    failed_cleanup_ok: Optional[bool] = None
    failed_cleanup_message: str = ""

    def as_dict(self) -> dict:
        return {
            "possible": self.possible,
            "reason": self.reason,
            "strategy": self.strategy,
            "branch_name": self.branch_name,
            "worktree_path": self.worktree_path,
            "start_sha": self.start_sha,
            "blockers": self.blockers,
        }


def plan_isolation(root: Path, task_id: str) -> IsolationPlan:
    """Decide how (or whether) this task can be isolated.

    Refuses rather than improvises. Every "no" names its cause, because
    "JARVIS could not start" with no reason is the least useful failure a
    tool can produce.
    """
    state = status(root)

    if not state.is_repository:
        return IsolationPlan(
            possible=False,
            reason=(
                "This folder is not a Git repository, so JARVIS cannot create an "
                "isolated branch for the task. Changes would be made directly to "
                "your files with no version-control safety net."
            ),
            strategy="in_place",
            blockers=["not_a_repository"],
        )

    blockers: List[str] = []
    if state.has_conflicts:
        blockers.append("unresolved_merge_conflicts")
    if state.detached:
        blockers.append("detached_head")

    if blockers:
        detail = {
            "unresolved_merge_conflicts": (
                "the repository has unresolved merge conflicts — finish or abandon "
                "the merge first, because a worktree created now would inherit them"
            ),
            "detached_head": (
                "HEAD is detached — creating a branch from here is possible but "
                "would silently change what you are working on"
            ),
        }
        return IsolationPlan(
            possible=False,
            reason="JARVIS did not start: " + "; ".join(detail[b] for b in blockers) + ".",
            strategy="none",
            blockers=blockers,
        )

    code, head, _ = _git(root, ["rev-parse", "HEAD"])
    if code != 0 or not head.strip():
        return IsolationPlan(
            possible=False,
            reason=(
                "This Git repository has no commit yet, so there is no revision "
                "from which JARVIS can create an isolated worktree."
            ),
            strategy="in_place",
            blockers=["no_initial_commit"],
        )

    branch = f"jarvis/task-{task_id[:8]}"
    worktree = root.parent / f".jarvis-worktrees/{root.name}-{task_id[:8]}"
    return IsolationPlan(
        possible=True,
        reason=(
            f"JARVIS will work in a separate Git worktree on branch '{branch}'. "
            "Your working copy, including anything you have changed but not "
            "committed, is not touched."
        ),
        strategy="worktree",
        branch_name=branch,
        worktree_path=str(worktree),
        start_sha=head.strip(),
    )


def create_worktree(root: Path, plan: IsolationPlan) -> Tuple[bool, str]:
    """Create the isolated worktree described by *plan*.

    `git worktree add -b <branch>` creates the branch from the current
    HEAD in a new directory. It does not modify the user's working copy
    and cannot discard anything: a worktree is additive.
    """
    if not plan.possible or plan.strategy != "worktree":
        return False, "No worktree was planned."
    if not plan.branch_name or not plan.worktree_path or not plan.start_sha:
        return False, "The isolation plan is incomplete."

    destination = Path(plan.worktree_path)
    if destination.exists():
        return False, f"'{destination.name}' already exists; JARVIS will not reuse it."

    ref = f"refs/heads/{plan.branch_name}"
    code, _, _ = _git(root, ["rev-parse", "--verify", ref])
    if code == 0:
        return False, (
            f"The planned branch '{plan.branch_name}' already exists; "
            "JARVIS will not reuse or remove it."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    plan.creation_attempted = True
    code, _, err = _git(
        root,
        ["worktree", "add", "-b", plan.branch_name, str(destination), plan.start_sha],
        timeout=60.0,
    )
    if code != 0:
        cleaned, cleanup_message = cleanup_failed_isolation(
            root, plan, allow_partial=True
        )
        plan.failed_cleanup_ok = cleaned
        plan.failed_cleanup_message = cleanup_message
        cleanup_note = (
            " Partial worktree/branch cleanup succeeded."
            if cleaned else f" Cleanup was refused: {cleanup_message}"
        )
        return False, (
            f"The worktree could not be created: {err.strip()[:200]}"
            + cleanup_note
        )
    logger.info("Coding worktree created for task on branch %s.", plan.branch_name)
    return True, f"Working in an isolated worktree on '{plan.branch_name}'."


def remove_worktree(root: Path, worktree_path: str, *, force_when_dirty: bool = False) -> Tuple[bool, str]:
    """Remove a task worktree — but never one that still holds changes.

    `force_when_dirty` exists so the caller has to say so explicitly, and
    even then this refuses unless the caller has separately confirmed the
    changes are JARVIS's own and unwanted. Automatic cleanup of a dirty
    worktree would throw away exactly the work the task produced.
    """
    destination = Path(worktree_path)
    if not destination.exists():
        return True, "Already removed."

    dirty = status(destination)
    if dirty.is_dirty and not force_when_dirty:
        return False, (
            "The task worktree still has uncommitted changes, so it was kept. "
            "Review or commit them first."
        )

    args = ["worktree", "remove", str(destination)]
    if force_when_dirty:
        args.insert(2, "--force")
    code, _, err = _git(root, args, timeout=60.0)
    if code != 0:
        return False, f"The worktree could not be removed: {err.strip()[:200]}"
    return True, "Task worktree removed."


def cleanup_failed_isolation(
    root: Path,
    plan: IsolationPlan,
    *,
    allow_partial: bool = False,
) -> Tuple[bool, str]:
    """Remove only the untouched worktree and branch this failed start made.

    The branch ref is deleted with its expected old SHA, so a concurrent
    commit makes cleanup refuse rather than discard it.
    """
    if (
        plan.strategy != "worktree"
        or not plan.branch_name
        or not plan.worktree_path
        or not plan.start_sha
        or not plan.branch_name.startswith("jarvis/task-")
    ):
        return False, "The failed task's isolation record was incomplete; it was kept."

    expected_parent = (root.parent / ".jarvis-worktrees").resolve(strict=False)
    destination = Path(plan.worktree_path).resolve(strict=False)
    if destination.parent != expected_parent:
        return False, "The failed task's worktree path was unexpected; it was kept."

    if destination.exists():
        registered = any(
            Path(entry.get("worktree", "")).resolve(strict=False) == destination
            for entry in worktrees(root)
            if entry.get("worktree")
        )
        if registered:
            removed, message = remove_worktree(
                root, str(destination), force_when_dirty=allow_partial
            )
            if not removed:
                return False, message
        elif allow_partial:
            marker = destination / ".git"
            try:
                is_empty = not any(destination.iterdir())
            except OSError:
                is_empty = False
            if marker.exists() or is_empty:
                try:
                    shutil.rmtree(destination)
                except OSError as exc:
                    return False, (
                        "The partial worktree directory could not be removed "
                        f"({type(exc).__name__})."
                    )
            else:
                return False, (
                    "The failed worktree path contains data that cannot be proved "
                    "to belong to Git, so JARVIS kept it."
                )
        else:
            return False, (
                "The worktree is not registered to this repository, so JARVIS kept it."
            )

    ref = f"refs/heads/{plan.branch_name}"
    code, current, _ = _git(root, ["rev-parse", "--verify", ref])
    if code != 0:
        return True, "Failed-start worktree removed; its branch was already absent."
    if current.strip() != plan.start_sha:
        return False, (
            "The failed task's branch moved after it was created, so JARVIS kept "
            "it rather than discarding work."
        )

    code, _, err = _git(root, ["update-ref", "-d", ref, plan.start_sha])
    if code != 0:
        return False, f"The worktree was removed but its branch could not be removed: {err.strip()[:200]}"
    return True, "Failed-start worktree and branch removed."


# --------------------------------------------------------------------------
# Commit proposal — shown, then approved, then run. Never the other way.
# --------------------------------------------------------------------------

@dataclass
class CommitProposal:
    message: str
    paths: List[str]
    branch: Optional[str]
    worktree: Optional[str]

    def as_dict(self) -> dict:
        return {
            "message": self.message,
            "paths": self.paths,
            "branch": self.branch,
            "worktree": self.worktree,
            "will_push": False,
        }


def build_commit_proposal(root: Path, message: str, paths: List[str]) -> CommitProposal:
    state = status(root)
    return CommitProposal(
        message=message.strip()[:2000],
        paths=sorted(set(paths)),
        branch=state.branch,
        worktree=str(root),
    )


def commit(root: Path, proposal: CommitProposal, *, approved: bool) -> Tuple[bool, str]:
    """Stage the named paths and commit them.

    `approved` is not advisory. Without it this returns a refusal and runs
    no git command at all — the check is before the first subprocess call,
    not inside a branch that could be reordered later.
    """
    if not approved:
        return False, "The commit was not approved, so nothing was committed."
    if not proposal.paths:
        return False, "There is nothing to commit."
    if not proposal.message.strip():
        return False, "A commit needs a message."

    # `--` separates paths from revisions, so a file called `main` cannot
    # be read as a branch name.
    code, _, err = _git(root, ["add", "--", *proposal.paths], timeout=60.0)
    if code != 0:
        return False, f"Staging failed: {err.strip()[:200]}"

    code, out, err = _git(
        root,
        ["commit", "-m", proposal.message, "--no-verify", "--", *proposal.paths],
        timeout=60.0,
    )
    if code != 0:
        return False, f"The commit failed: {(err or out).strip()[:200]}"
    logger.info("Coding task commit created on %s.", proposal.branch)
    return True, "Committed locally. Nothing was pushed."


def undo_task_edits(root: Path, task_id: str, changes: List[dict]) -> List[dict]:
    """Restore this task's exact pre-edit bytes and paths, in reverse order.

    No Git restore/reset command is used.  Each current path must still
    match the hash JARVIS recorded after its own operation; otherwise the
    user's newer version wins and that change is skipped.
    """
    from app.coding.undo import restore_task_changes

    return restore_task_changes(root, task_id, changes)
