"""The editing engine: what it preserves, and what it refuses.

The failure this guards against is not "the edit did not apply". It is
"the edit applied, and quietly destroyed something": a file's encoding,
its line endings, a change the user made in another window thirty
seconds ago, or a binary that is now UTF-8 mojibake.
"""

import hashlib
from pathlib import Path

import pytest

from app.coding import editing, limits
from app.coding.editing import EditKind, EditProposal
from app.coding.workspace import WorkspaceViolation, resolve
from tests import coding_fixtures as fx


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    fx.static_site(root, with_defect=False)
    fx.with_secrets(root)
    return root


def sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def test_an_update_writes_the_new_content_and_reports_the_diff(project):
    target = project / "index.html"
    before = sha_of(target)
    result = editing.apply(project, EditProposal(
        kind=EditKind.UPDATE, path="index.html",
        new_text="<!doctype html>\n<h1>Changed</h1>\n",
        base_sha256=before, reason="test",
    ))
    assert result.applied is True
    assert "Changed" in target.read_text(encoding="utf-8")
    assert result.before_sha256 == before
    assert result.after_sha256 == sha_of(target)
    assert result.lines_added > 0
    assert "+<h1>Changed</h1>" in result.diff


def test_preview_shows_the_diff_and_changes_nothing(project):
    target = project / "index.html"
    before = target.read_bytes()
    result = editing.preview(project, EditProposal(
        kind=EditKind.UPDATE, path="index.html",
        new_text="<h1>Different</h1>\n", base_sha256=sha_of(target),
    ))
    assert result.diff
    assert target.read_bytes() == before, "preview must not write"


def test_creating_a_file_that_already_exists_is_refused(project):
    result = editing.apply(project, EditProposal(
        kind=EditKind.CREATE, path="index.html", new_text="x\n"))
    assert result.applied is False
    assert "exists" in result.message.lower()


def test_a_rename_moves_the_file_and_leaves_nothing_behind(project):
    result = editing.apply(project, EditProposal(
        kind=EditKind.RENAME, path="index.html", destination="home.html"))
    assert result.applied is True
    assert (project / "home.html").exists()
    assert not (project / "index.html").exists()


# ---------------------------------------------------------------------------
# Staleness — the important one
# ---------------------------------------------------------------------------

def test_a_patch_against_a_stale_base_is_refused(project):
    """Somebody edited the file in their editor between the model reading
    it and proposing a change. Overwriting would silently discard their
    work."""
    target = project / "index.html"
    stale = sha_of(target)

    target.write_text("<h1>The user typed this</h1>\n", encoding="utf-8")

    result = editing.apply(project, EditProposal(
        kind=EditKind.UPDATE, path="index.html",
        new_text="<h1>Model's version</h1>\n", base_sha256=stale,
    ))
    assert result.applied is False
    assert "changed" in result.message.lower()
    assert target.read_text(encoding="utf-8") == "<h1>The user typed this</h1>\n", (
        "the user's own edit must survive intact"
    )


def test_a_patch_with_a_matching_base_applies(project):
    target = project / "index.html"
    result = editing.apply(project, EditProposal(
        kind=EditKind.UPDATE, path="index.html", new_text="<h1>Fine</h1>\n",
        base_sha256=sha_of(target)))
    assert result.applied is True


# ---------------------------------------------------------------------------
# Preservation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("newline,label", [("\r\n", "CRLF"), ("\n", "LF")])
def test_line_endings_survive_an_edit(project, newline, label):
    target = project / "config.txt"
    target.write_bytes(f"alpha{newline}beta{newline}".encode("utf-8"))
    result = editing.apply(project, EditProposal(
        kind=EditKind.UPDATE, path="config.txt",
        new_text="alpha\nbeta\ngamma\n", base_sha256=sha_of(target)))
    assert result.applied is True
    raw = target.read_bytes()
    if label == "CRLF":
        assert b"\r\n" in raw and raw.count(b"\r\n") == raw.count(b"\n"), (
            "a CRLF file must not come back as LF"
        )
    else:
        assert b"\r\n" not in raw


def test_a_utf8_bom_survives_an_edit(project):
    target = project / "bom.txt"
    target.write_bytes(b"\xef\xbb\xbfhello\n")
    result = editing.apply(project, EditProposal(
        kind=EditKind.UPDATE, path="bom.txt", new_text="hello there\n",
        base_sha256=sha_of(target)))
    assert result.applied is True
    assert target.read_bytes().startswith(b"\xef\xbb\xbf"), "the BOM was dropped"


def test_non_utf8_content_is_read_and_written_back_in_its_own_encoding(project):
    target = project / "legacy.txt"
    target.write_bytes("café costs 5£\n".encode("cp1252"))
    snapshot = editing.read_snapshot(resolve(project, "legacy.txt"))
    assert "café" in snapshot.text
    result = editing.apply(project, EditProposal(
        kind=EditKind.UPDATE, path="legacy.txt", new_text="café costs 6£\n",
        base_sha256=snapshot.sha256))
    assert result.applied is True
    assert target.read_bytes().decode("cp1252") == "café costs 6£\n"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def test_a_binary_file_is_never_edited(project):
    target = project / "logo.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x01")
    original = target.read_bytes()
    result = editing.apply(project, EditProposal(
        kind=EditKind.UPDATE, path="logo.png", new_text="not a png"))
    assert result.applied is False
    assert "binary" in result.message.lower()
    assert target.read_bytes() == original


def test_a_protected_file_is_never_written(project):
    """A refusal, not an exception. `apply()` turns a WorkspaceViolation
    into a refused result carrying a message the user can read — the
    caller gets data rather than a traceback — and the bytes on disk are
    what actually matters here."""
    original = (project / ".env").read_bytes()
    result = editing.apply(project, EditProposal(
        kind=EditKind.UPDATE, path=".env", new_text="ANTHROPIC_API_KEY=stolen\n"))
    assert result.applied is False
    assert "protected" in result.message.lower()
    assert (project / ".env").read_bytes() == original


def test_a_path_outside_the_project_is_never_written(project, tmp_path):
    outsider = tmp_path / "victim.txt"
    outsider.write_text("theirs\n", encoding="utf-8")
    result = editing.apply(project, EditProposal(
        kind=EditKind.UPDATE, path="../victim.txt", new_text="mine\n"))
    assert result.applied is False
    assert "outside" in result.message.lower()
    assert outsider.read_text(encoding="utf-8") == "theirs\n"


def test_resolve_itself_still_raises_for_a_protected_path(project):
    """The refusal above is `apply()` being a good citizen. The boundary
    underneath it still raises, so a future caller that forgets to check
    a result fails loudly rather than silently proceeding."""
    with pytest.raises(WorkspaceViolation):
        resolve(project, ".env")


def test_deletion_without_approval_is_refused(project):
    result = editing.apply(project, EditProposal(kind=EditKind.DELETE, path="index.html"))
    assert result.applied is False
    assert (project / "index.html").exists()


def test_deletion_with_approval_removes_the_file(project):
    result = editing.apply(project, EditProposal(kind=EditKind.DELETE, path="style.css"),
                           approved_delete=True)
    assert result.applied is True
    assert not (project / "style.css").exists()


def test_an_oversized_file_is_refused(project):
    target = project / "huge.txt"
    target.write_bytes(b"x" * (limits.MAX_EDITABLE_FILE_BYTES + 1))
    result = editing.apply(project, EditProposal(
        kind=EditKind.UPDATE, path="huge.txt", new_text="small"))
    assert result.applied is False
    assert result.proposal.path == "huge.txt"


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------

def test_apply_all_stops_at_the_file_edit_limit(project):
    budget = limits.TaskBudget(files_edited=2)
    proposals = [
        EditProposal(kind=EditKind.CREATE, path=f"new{i}.txt", new_text="x\n")
        for i in range(5)
    ]
    batch = editing.apply_all(project, proposals, budget)
    assert len(batch.applied) == 2
    assert len(batch.refused) == 3
    assert any("limit" in r.message.lower() for r in batch.refused)


def test_the_limit_message_names_the_budget_actually_in_force():
    """A budget of 2 must not announce the module default of 25."""
    budget = limits.TaskBudget(files_edited=1)
    assert budget.spend_file_edit() is None
    reason = budget.spend_file_edit()
    assert reason is not None
    assert "1-file" in reason, reason
    assert str(limits.MAX_FILES_EDITED) not in reason


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------

def test_a_write_leaves_no_temporary_file_behind(project):
    editing.apply(project, EditProposal(
        kind=EditKind.UPDATE, path="index.html", new_text="<h1>x</h1>\n",
        base_sha256=sha_of(project / "index.html")))
    leftovers = [p.name for p in project.iterdir() if ".tmp" in p.name or p.name.endswith("~")]
    assert leftovers == [], f"temporary files left behind: {leftovers}"


def test_the_temporary_file_is_written_beside_the_target(project, monkeypatch):
    """os.replace is atomic only within one filesystem. A temp file in the
    system temp directory can be on a different one, which turns the
    atomic rename into a copy that can be interrupted halfway."""
    seen = {}
    real_replace = editing.os.replace

    def spy(src, dst):
        seen["src_parent"] = Path(src).parent.resolve()
        seen["dst_parent"] = Path(dst).parent.resolve()
        return real_replace(src, dst)

    monkeypatch.setattr(editing.os, "replace", spy)
    editing.apply(project, EditProposal(
        kind=EditKind.UPDATE, path="index.html", new_text="<h1>y</h1>\n",
        base_sha256=sha_of(project / "index.html")))
    assert seen["src_parent"] == seen["dst_parent"]
