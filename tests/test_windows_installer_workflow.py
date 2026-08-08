"""Sanity checks for .github/workflows/windows-installer.yml.

Structural checks only — this cannot run the workflow itself (that only
really executes on a real windows-latest GitHub Actions runner). Guards
against the properties that matter most for this specific workflow: it
never publishes a public release (that stays exclusively in the
pre-existing, separately-triggered .github/workflows/release.yml, which
this workflow does not call or modify), it pins the same Inno Setup
version packaging/jarvis.iss documents, and it runs the build script
before the clean-install test (not the other way around, and not
independently of each other).
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "windows-installer.yml"


def _read() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _parsed() -> dict:
    return yaml.safe_load(_read())


def test_file_exists():
    assert WORKFLOW_PATH.exists()


def test_is_valid_yaml():
    data = _parsed()
    assert "jobs" in data


def test_runs_on_windows_latest():
    data = _parsed()
    job = data["jobs"]["build-and-test-installer"]
    assert job["runs-on"] == "windows-latest"


def test_pins_the_same_inno_setup_version_as_jarvis_iss():
    """6.7.1, not upstream's own latest (6.7.3) — Chocolatey's community
    package repository, which is what this workflow actually installs
    from, lags upstream and does not carry 6.7.3. An earlier pin at
    6.7.3 here failed real CI for exactly that reason ("the package was
    not found with the source(s) listed"); see packaging/jarvis.iss's
    header comment for the verified detail."""
    iss_content = (REPO_ROOT / "packaging" / "jarvis.iss").read_text(encoding="utf-8")
    assert "6.7.1" in iss_content  # sanity: the version this workflow must match
    assert "--version=6.7.1" in _read()


def test_build_script_runs_before_clean_install_test():
    data = _parsed()
    steps = data["jobs"]["build-and-test-installer"]["steps"]
    run_steps = [s.get("run", "") for s in steps if "run" in s]
    build_index = next(i for i, r in enumerate(run_steps) if "build-installer.ps1" in r)
    test_index = next(i for i, r in enumerate(run_steps) if "test_clean_install.py" in r)
    assert build_index < test_index


def test_never_publishes_a_release():
    """Publishing stays exclusively in the separate, manually-triggered
    .github/workflows/release.yml — this workflow must never call `gh
    release create` or any release-publishing action itself."""
    content = _read()
    assert "release create" not in content
    assert "softprops/action-gh-release" not in content
    assert "actions/create-release" not in content


def test_does_not_use_workflow_dispatch_only_release_job():
    """This is a genuinely separate job/workflow from release.yml, not a
    dependency on or trigger for it."""
    data = _parsed()
    assert "release" not in data["jobs"]  # no job named/aliased "release" here


def test_uploads_installer_and_checksum():
    content = _read()
    assert "JARVIS-Setup-*.exe" in content
    assert "JARVIS-Setup-*.exe.sha256" in content


def test_path_scoped_not_unconditional():
    """Heavier/slower than ci.yml's fast per-PR jobs — should only run
    when packaging-relevant paths actually change (plus workflow_dispatch
    for an explicit on-demand build), not on every unrelated PR."""
    data = _parsed()
    on = data[True] if True in data else data.get("on", {})
    assert "workflow_dispatch" in on
    assert "paths" in on["pull_request"]
    assert "packaging/**" in on["pull_request"]["paths"]
