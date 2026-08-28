"""The workspace boundary — the one thing that must not have a hole.

Every test here is an attack, not a happy path. A path that escapes the
project root reaches the user's SSH keys, their browser profile and
JARVIS's own database, so the interesting question is never "does a
normal path work" but "does an abnormal one fail".
"""

import os
import sys
from pathlib import Path, PurePath, PureWindowsPath

import pytest

from app.coding.workspace import (
    WorkspaceViolation,
    _is_within,
    canonical_root,
    is_protected,
    iter_project_files,
    protected_summary,
    resolve,
    safe_metadata,
)
from tests import coding_fixtures as fx


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    fx.static_site(root)
    fx.with_secrets(root)
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "app.js").write_text("console.log('hi');\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------

def test_an_ordinary_file_inside_the_project_resolves(project):
    resolved = resolve(project, "src/app.js", must_exist=True)
    assert resolved.absolute == (project / "src" / "app.js").resolve()
    assert resolved.display == "src/app.js"


@pytest.mark.parametrize("candidate", [
    "../outside.txt",
    "../../outside.txt",
    "src/../../outside.txt",
    "./../../etc/passwd",
    "src/./../../../../../../etc/shadow",
    "..",
    "../",
])
def test_traversal_is_refused(project, candidate):
    with pytest.raises(WorkspaceViolation):
        resolve(project, candidate)


def test_an_absolute_path_outside_the_project_is_refused(project, tmp_path):
    outsider = tmp_path / "elsewhere.txt"
    outsider.write_text("not yours\n", encoding="utf-8")
    with pytest.raises(WorkspaceViolation):
        resolve(project, str(outsider))


def test_a_sibling_whose_name_starts_with_the_root_name_is_outside(tmp_path):
    """`/tmp/proj-evil` is not inside `/tmp/proj`.

    A string-prefix check says it is. This is the specific bug that makes
    prefix comparison the wrong tool, so it is asserted directly against
    the helper as well as through resolve().
    """
    root = tmp_path / "proj"
    root.mkdir()
    evil = tmp_path / "proj-evil"
    evil.mkdir()
    (evil / "loot.txt").write_text("x\n", encoding="utf-8")

    assert str(evil).startswith(str(root)), "the fixture must actually share the prefix"
    assert _is_within(evil.resolve(), root.resolve()) is False

    with pytest.raises(WorkspaceViolation):
        resolve(root, str(evil / "loot.txt"))


@pytest.mark.parametrize("candidate", [
    r"\\.\PhysicalDrive0",
    "//./PhysicalDrive0",
    r"\\?\C:\Windows\System32\config\SAM",
    "//?/C:/Windows",
    r"\\server\share\secret.txt",
    "//server/share/secret.txt",
])
def test_device_unc_and_namespace_paths_are_refused(project, candidate):
    with pytest.raises(WorkspaceViolation):
        resolve(project, candidate)


@pytest.mark.parametrize("candidate", ["notes.txt:hidden", "src/app.js:$DATA"])
def test_alternate_data_streams_are_refused(project, candidate):
    """NTFS lets `file.txt:hidden` carry content the file listing never
    shows. Refused on every platform, so a Linux CI run still checks it."""
    with pytest.raises(WorkspaceViolation):
        resolve(project, candidate)


@pytest.mark.parametrize("name", ["CON", "con", "PRN", "NUL", "aux", "COM1", "lpt9"])
def test_reserved_windows_device_names_are_refused(project, name):
    with pytest.raises(WorkspaceViolation):
        resolve(project, name)


def test_an_empty_or_whitespace_path_is_refused(project):
    for candidate in ("", "   ", "\t"):
        with pytest.raises(WorkspaceViolation):
            resolve(project, candidate)


def test_a_null_byte_is_refused(project):
    with pytest.raises(WorkspaceViolation):
        resolve(project, "src/app.js\x00.png")


def test_must_exist_refuses_a_missing_file(project):
    with pytest.raises(WorkspaceViolation):
        resolve(project, "no-such-file.txt", must_exist=True)
    # ...but not when the caller is about to create it.
    assert resolve(project, "no-such-file.txt").display == "no-such-file.txt"


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
def test_a_symlink_pointing_outside_the_project_is_refused(project, tmp_path):
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("private\n", encoding="utf-8")
    link = project / "innocent.txt"
    link.symlink_to(secret)

    assert link.exists(), "the fixture link must resolve, or this proves nothing"
    with pytest.raises(WorkspaceViolation):
        resolve(project, "innocent.txt", must_exist=True)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
def test_a_symlinked_directory_pointing_outside_is_refused(project, tmp_path):
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "loot.txt").write_text("private\n", encoding="utf-8")
    (project / "vendor").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceViolation):
        resolve(project, "vendor/loot.txt", must_exist=True)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
def test_a_symlink_inside_the_project_is_allowed(project):
    (project / "alias.js").symlink_to(project / "src" / "app.js")
    resolved = resolve(project, "alias.js", must_exist=True)
    assert _is_within(resolved.absolute, project.resolve())


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
def test_the_root_itself_being_replaced_by_a_link_is_caught(tmp_path):
    """The root is re-canonicalised on every call, so a folder swapped for
    a link to somewhere else after the project was registered is not
    silently followed."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "file.txt").write_text("mine\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "file.txt").write_text("not mine\n", encoding="utf-8")

    registered = tmp_path / "project"
    registered.symlink_to(real, target_is_directory=True)
    assert canonical_root(registered) == real.resolve()

    registered.unlink()
    registered.symlink_to(elsewhere, target_is_directory=True)
    assert canonical_root(registered) == elsewhere.resolve(), (
        "canonical_root must recompute, not remember"
    )


# ---------------------------------------------------------------------------
# Protected files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("relative", [
    ".env", ".env.production", ".env.local", ".npmrc", ".netrc",
    "credentials.json", "secrets.yaml", "certs/server.key", "id_rsa",
    ".git/config", ".ssh/id_ed25519", ".aws/credentials", "app.private.json",
])
def test_credential_shaped_paths_are_protected(relative):
    assert is_protected(PurePath(relative)) is not None, f"{relative} must be protected"


@pytest.mark.parametrize("relative", [
    "src/app.js", "README.md", "package.json", "index.html",
    "environment.md", "secretary.py", "keyboard.ts", "src/env-utils.ts",
])
def test_ordinary_files_are_not_protected(relative):
    assert is_protected(PurePath(relative)) is None, f"{relative} must be readable"


def test_resolving_a_protected_file_is_refused_by_default(project):
    with pytest.raises(WorkspaceViolation):
        resolve(project, ".env")


def test_a_protected_file_can_be_located_but_only_for_metadata(project):
    """`allow_protected` exists so the *existence* can be reported. It is
    the only caller-visible way to name one, and nothing that reads bytes
    passes it."""
    resolved = resolve(project, ".env", allow_protected=True)
    assert resolved.display == ".env"
    metadata = safe_metadata(project, ".env")
    assert metadata["protected"] is True
    assert "size_bytes" in metadata
    # The whole point: no content, under any key.
    flattened = repr(metadata)
    for secret in fx.SECRET_VALUES:
        assert secret not in flattened


def test_listing_a_project_marks_protected_files_and_never_reads_them(project):
    entries = iter_project_files(project)
    by_path = {e["path"]: e for e in entries}
    assert by_path[".env"]["protected"] is True
    assert by_path["index.html"]["protected"] is False
    flattened = repr(entries)
    for secret in fx.SECRET_VALUES:
        assert secret not in flattened, "a listing must never carry file contents"


def test_git_internals_are_protected_wholesale(project):
    for candidate in (".git/config", ".git/HEAD", ".git/hooks/pre-commit"):
        assert is_protected(PurePath(candidate)) is not None


def test_the_protected_summary_matches_what_is_enforced():
    """The UI renders this. If it could list something is_protected()
    does not actually protect, the page would be making a promise the
    code does not keep."""
    summary = protected_summary()
    for name in summary["filenames"]:
        assert is_protected(PurePath(name)) is not None, f"{name} is listed but not enforced"
    for suffix in summary["suffixes"]:
        assert is_protected(PurePath(f"anything{suffix}")) is not None
    for directory in summary["directories"]:
        assert is_protected(PurePath(directory) / "anything") is not None


# ---------------------------------------------------------------------------
# Windows specifics that can still be checked on Linux
# ---------------------------------------------------------------------------

def test_drive_letter_casing_does_not_change_containment():
    """`C:\\Users\\x` and `c:\\users\\x` are the same place on Windows.
    A case-sensitive comparison would treat one as an escape from the
    other."""
    from app.coding.workspace import _components

    upper = _components(PureWindowsPath(r"C:\Users\Someone\project"))
    lower = _components(PureWindowsPath(r"c:\users\someone\project"))
    if os.name == "nt":
        assert upper == lower
    else:
        # On Linux the comparison is case-sensitive by design; assert the
        # helper is at least deterministic rather than asserting Windows
        # semantics a POSIX filesystem does not have.
        assert upper == _components(PureWindowsPath(r"C:\Users\Someone\project"))
