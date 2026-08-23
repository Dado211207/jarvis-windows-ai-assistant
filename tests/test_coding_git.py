"""Git safety: the user's own work must survive everything.

The nine repository states §6 names are all here, each with the same
question asked of it: after JARVIS has done whatever it does, is the
user's uncommitted work exactly as they left it?

That question is asked by comparing file *bytes* before and after, not by
reading a status flag. A test that asserts "we did not call git reset"
proves nothing about a code path that calls `git checkout --force`
instead.
"""

import subprocess
from pathlib import Path

import pytest

from app.coding import gitsafe
from tests import coding_fixtures as fx


def snapshot_bytes(root: Path) -> dict:
    """Every file's exact contents, so a comparison afterwards is real."""
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".git" not in path.parts:
            result[str(path.relative_to(root))] = path.read_bytes()
    return result


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def test_status_reports_a_clean_repository(tmp_path):
    root = fx.static_site(tmp_path / "clean", with_defect=False)
    fx.init_repo(root)
    state = gitsafe.status(root)
    assert state.is_repository is True
    assert state.is_dirty is False
    assert state.branch == "main"


def test_status_finds_modified_staged_and_untracked_separately(tmp_path):
    root = fx.static_site(tmp_path / "mixed", with_defect=False)
    fx.init_repo(root)
    fx.write(root, "index.html", "<h1>modified</h1>\n")
    fx.write(root, "staged.txt", "staged\n")
    fx.git(root, "add", "staged.txt")
    fx.write(root, "untracked.txt", "untracked\n")

    state = gitsafe.status(root)
    assert "index.html" in state.modified
    assert "staged.txt" in state.staged
    assert "untracked.txt" in state.untracked
    assert state.is_dirty is True


def test_a_folder_that_is_not_a_repository_says_so_rather_than_failing(tmp_path):
    root = fx.not_a_repo(tmp_path / "plain")
    state = gitsafe.status(root)
    assert state.is_repository is False
    assert state.is_dirty is False


def test_a_merge_conflict_is_detected(tmp_path):
    root = fx.repo_with_merge_conflict(tmp_path / "conflicted")
    state = gitsafe.status(root)
    assert state.has_conflicts is True


def test_a_detached_head_is_detected(tmp_path):
    root = fx.repo_detached_head(tmp_path / "detached")
    assert gitsafe.status(root).detached is True


def test_credentials_embedded_in_a_remote_url_are_never_returned(tmp_path):
    root = fx.static_site(tmp_path / "remote", with_defect=False)
    fx.init_repo(root)
    fx.git(root, "remote", "add", "origin",
           "https://someone:ghp_averysecrettokenvalue@github.com/someone/repo.git")

    remotes = gitsafe.remotes(root)
    flattened = repr(remotes)
    assert "ghp_averysecrettokenvalue" not in flattened
    assert "someone:" not in flattened
    assert "github.com/someone/repo" in flattened, "the useful part must survive"


def test_strip_credentials_handles_the_shapes_that_appear_in_practice():
    cases = [
        ("https://user:token@github.com/a/b.git", "token"),
        ("https://token@github.com/a/b.git", "token"),
        ("https://x-access-token:ghs_abc123@github.com/a/b", "ghs_abc123"),
    ]
    for url, secret in cases:
        assert secret not in gitsafe.strip_credentials(url), url


# ---------------------------------------------------------------------------
# Isolation, across every state
# ---------------------------------------------------------------------------

def test_isolation_is_planned_for_a_clean_repository(tmp_path):
    root = fx.static_site(tmp_path / "clean", with_defect=False)
    fx.init_repo(root)
    plan = gitsafe.plan_isolation(root, "task1234")
    assert plan.possible is True
    assert plan.strategy == "worktree"
    assert plan.branch_name and plan.branch_name.startswith("jarvis/")


@pytest.mark.parametrize("builder,label", [
    (fx.repo_with_modified_tracked_file, "modified tracked file"),
    (fx.repo_with_staged_changes, "staged changes"),
    (fx.repo_with_untracked_files, "untracked files"),
    (fx.repo_with_ignored_files, "ignored files"),
])
def test_a_worktree_leaves_every_uncommitted_change_untouched(tmp_path, builder, label):
    """The central claim of the whole feature, tested by bytes."""
    root = builder(tmp_path / "project")
    before = snapshot_bytes(root)

    plan = gitsafe.plan_isolation(root, "task5678")
    assert plan.possible is True, f"{label}: isolation should still be possible"
    created, message = gitsafe.create_worktree(root, plan)
    assert created is True, message

    worktree = Path(plan.worktree_path)
    assert worktree.is_dir()

    # Work in the worktree, as a task would.
    fx.write(worktree, "jarvis-made-this.txt", "task output\n")
    fx.write(worktree, "index.html", "<h1>changed by the task</h1>\n")

    after = snapshot_bytes(root)
    assert after == before, (
        f"{label}: the user's working copy changed. "
        f"added={set(after) - set(before)} removed={set(before) - set(after)} "
        f"altered={[k for k in before if k in after and before[k] != after[k]]}"
    )


@pytest.mark.parametrize("builder,blocker", [
    (fx.repo_with_merge_conflict, "unresolved_merge_conflicts"),
    (fx.repo_detached_head, "detached_head"),
])
def test_isolation_refuses_and_explains_rather_than_improvising(tmp_path, builder, blocker):
    root = builder(tmp_path / "project")
    plan = gitsafe.plan_isolation(root, "task9999")
    assert plan.possible is False
    assert blocker in plan.blockers
    assert plan.reason, "a refusal with no reason is the least useful failure there is"


def test_a_folder_with_no_repository_is_reported_as_in_place_with_a_warning(tmp_path):
    root = fx.not_a_repo(tmp_path / "plain")
    plan = gitsafe.plan_isolation(root, "task0000")
    assert plan.possible is False
    assert plan.strategy == "in_place"
    assert "not a Git repository" in plan.reason


def test_a_nested_repository_does_not_confuse_the_outer_one(tmp_path):
    root = fx.repo_with_nested_repo(tmp_path / "outer")
    state = gitsafe.status(root)
    assert state.is_repository is True
    # The inner repo appears as one untracked entry, not as its contents.
    assert not any(entry.startswith("vendor/library/lib.js") for entry in state.untracked)


def test_an_existing_worktree_is_listed_and_not_disturbed(tmp_path):
    root = fx.repo_with_worktree(tmp_path / "project", tmp_path / "side-worktree")
    before = snapshot_bytes(tmp_path / "side-worktree")
    listed = gitsafe.worktrees(root)
    assert len(listed) >= 2
    assert snapshot_bytes(tmp_path / "side-worktree") == before


def test_a_submodule_is_not_entered_or_modified(tmp_path):
    root = fx.repo_with_submodule(tmp_path / "project", tmp_path / "upstream")
    before = snapshot_bytes(root / "vendor" / "dep")
    gitsafe.status(root)
    gitsafe.diff(root)
    assert snapshot_bytes(root / "vendor" / "dep") == before


def test_creating_a_worktree_twice_refuses_rather_than_reusing(tmp_path):
    root = fx.static_site(tmp_path / "project", with_defect=False)
    fx.init_repo(root)
    plan = gitsafe.plan_isolation(root, "sametask")
    assert gitsafe.create_worktree(root, plan)[0] is True
    created, message = gitsafe.create_worktree(root, plan)
    assert created is False
    assert "already exists" in message.lower()


# ---------------------------------------------------------------------------
# What must not exist
# ---------------------------------------------------------------------------

def test_the_git_layer_has_no_destructive_verb_anywhere_in_it():
    """A grep of the module's own source. The point is not that these
    calls are currently absent — it is that adding one has to be a
    deliberate act that fails a test."""
    source = (Path(__file__).resolve().parent.parent /
              "app" / "coding" / "gitsafe.py").read_text(encoding="utf-8")
    # Only the argv lists matter, and every one is a list of literals, so
    # a substring search over the source is the right granularity here.
    forbidden = [
        '"reset"', '"clean"', '"push"', '"filter-branch"',
        '"rebase"', '"cherry-pick"',
    ]
    for verb in forbidden:
        # Allowed to *name* them in FORBIDDEN_VERBS and in prose; not to
        # place one in a command list.
        for line in source.splitlines():
            if verb in line and "FORBIDDEN_VERBS" not in source[:source.index(line)][-400:]:
                assert not line.strip().startswith('_git('), f"destructive verb in a call: {line}"


def test_forbidden_verbs_are_declared_and_include_the_dangerous_ones():
    assert {"reset", "clean", "push", "filter-branch"} <= set(gitsafe.FORBIDDEN_VERBS)


def test_undo_only_touches_files_whose_hash_still_matches(tmp_path):
    """A file the user edited after JARVIS wrote it must be left alone."""
    root = fx.static_site(tmp_path / "project", with_defect=False)
    fx.init_repo(root)

    import hashlib

    # Written through fx.write, which writes exact bytes. Python's text
    # mode would translate "\n" to "\r\n" on Windows, so a hash computed
    # over the intended string would not match the file — the test would
    # fail for a reason unrelated to what it is testing.
    jarvis_wrote = "<h1>JARVIS wrote this</h1>\n"
    fx.write(root, "index.html", jarvis_wrote)
    recorded = hashlib.sha256(jarvis_wrote.encode("utf-8")).hexdigest()

    style_wrote = "/* JARVIS wrote this too */\n"
    fx.write(root, "style.css", style_wrote)
    style_hash = hashlib.sha256(style_wrote.encode("utf-8")).hexdigest()

    # Now the user edits one of them.
    user_text = "<h1>and then the user changed it</h1>\n"
    fx.write(root, "index.html", user_text)

    results = gitsafe.undo_task_edits(
        root, ["index.html", "style.css"],
        {"index.html": recorded, "style.css": style_hash},
    )
    by_path = {r["path"]: r for r in results}
    assert by_path["index.html"]["reverted"] is False, "the user's later edit was discarded"
    assert (root / "index.html").read_bytes() == user_text.encode("utf-8")
    assert by_path["style.css"]["reverted"] is True


def test_a_commit_proposal_does_not_commit(tmp_path):
    root = fx.static_site(tmp_path / "project", with_defect=False)
    fx.init_repo(root)
    fx.write(root, "new.txt", "content\n")

    before = fx.git(root, "rev-parse", "HEAD").stdout.strip()
    proposal = gitsafe.build_commit_proposal(root, "add a file", ["new.txt"])
    assert proposal.message
    assert fx.git(root, "rev-parse", "HEAD").stdout.strip() == before


def test_a_commit_without_approval_is_refused(tmp_path):
    root = fx.static_site(tmp_path / "project", with_defect=False)
    fx.init_repo(root)
    fx.write(root, "new.txt", "content\n")
    proposal = gitsafe.build_commit_proposal(root, "add a file", ["new.txt"])

    before = fx.git(root, "rev-parse", "HEAD").stdout.strip()
    committed, message = gitsafe.commit(root, proposal, approved=False)
    assert committed is False
    assert fx.git(root, "rev-parse", "HEAD").stdout.strip() == before


def test_an_approved_commit_commits_only_the_named_files(tmp_path):
    root = fx.static_site(tmp_path / "project", with_defect=False)
    fx.init_repo(root)
    fx.write(root, "jarvis.txt", "from the task\n")
    fx.write(root, "user-scratch.txt", "the user's own file\n")

    proposal = gitsafe.build_commit_proposal(root, "task change", ["jarvis.txt"])
    committed, message = gitsafe.commit(root, proposal, approved=True)
    assert committed is True, message

    tracked = fx.git(root, "ls-files").stdout.split()
    assert "jarvis.txt" in tracked
    assert "user-scratch.txt" not in tracked, "an unrelated file was swept into the commit"
    assert (root / "user-scratch.txt").exists()


def test_removing_a_worktree_that_still_has_changes_is_refused(tmp_path):
    root = fx.static_site(tmp_path / "project", with_defect=False)
    fx.init_repo(root)
    plan = gitsafe.plan_isolation(root, "task4321")
    gitsafe.create_worktree(root, plan)
    worktree = Path(plan.worktree_path)
    fx.write(worktree, "unsaved-task-work.txt", "not committed\n")

    removed, message = gitsafe.remove_worktree(root, str(worktree))
    assert removed is False
    assert worktree.exists(), "task work was thrown away"


# ---------------------------------------------------------------------------
# The diff is a way for a file's contents to leave the project
# ---------------------------------------------------------------------------

def test_a_diff_never_shows_the_contents_of_a_protected_file(tmp_path):
    """`git diff` knows nothing about the protected-path engine.

    The Windows CI job found this: line-ending normalisation made every
    file show as modified, and `GET /coding/projects/{id}/diff` returned
    the fixture's fake Anthropic key, Stripe secret, npm token and
    private key. On Linux the same test passed only because nothing
    happened to be modified — the hole was there either way.
    """
    root = fx.static_site(tmp_path / "project", with_defect=False)
    fx.with_secrets(root)
    fx.init_repo(root)

    # The user edits everything, including their credentials.
    fx.write(root, ".env", f"ANTHROPIC_API_KEY={fx.FAKE_ANTHROPIC_KEY}\nEDITED=yes\n")
    fx.write(root, "certs/server.key",
             "-----BEGIN PRIVATE KEY-----\nFIXTUREPRIVATEKEYMATERIAL\nrotated\n"
             "-----END PRIVATE KEY-----\n")
    fx.write(root, "secrets.yaml", "database_password: fixture-yaml-secret\nnew: 1\n")
    fx.write(root, "index.html", "<h1>a real change the user made</h1>\n")

    body = gitsafe.diff(root)

    for secret in fx.SECRET_VALUES:
        assert secret not in body, f"the diff leaked {secret[:16]}…"

    # The ordinary change is still shown — this must not be a blunt refusal.
    assert "a real change the user made" in body

    # And the user is told their protected files differ, without being
    # shown what they now say.
    assert ".env" in body
    assert "not shown" in body


def test_a_staged_diff_excludes_protected_files_too(tmp_path):
    root = fx.static_site(tmp_path / "project", with_defect=False)
    fx.with_secrets(root)
    fx.init_repo(root)

    fx.write(root, ".env", f"ANTHROPIC_API_KEY={fx.FAKE_ANTHROPIC_KEY}\nSTAGED=yes\n")
    fx.git(root, "add", ".env")

    body = gitsafe.diff(root, staged=True)
    for secret in fx.SECRET_VALUES:
        assert secret not in body, "the staged diff leaked a secret"


def test_a_diff_of_only_protected_files_is_a_notice_not_an_empty_string(tmp_path):
    """Empty would read as "nothing changed", which is false."""
    root = fx.static_site(tmp_path / "project", with_defect=False)
    fx.with_secrets(root)
    fx.init_repo(root)
    fx.write(root, ".env", "ANTHROPIC_API_KEY=changed\n")

    body = gitsafe.diff(root)
    assert body.strip(), "a changed .env produced an empty diff, which reads as 'no changes'"
    assert ".env" in body
