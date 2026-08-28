"""Toolchain diagnostics: look, do not search, do not change, do not lie.

Three claims, in order of how much they matter.

**A missing tool is never a pass.** A validation step whose tool is not
installed did not agree the code is fine; it did not look. That is the
same defect as reporting zero console errors for a page nothing opened,
one subsystem across.

**A repository does not get to choose which Git runs.** Windows' PATH
resolution has historically consulted the current directory, and a
`git.exe` committed to a project is a project supplying its own
toolchain. Every resolution is checked and refused.

**Nothing is installed and nothing is changed**, proved structurally
rather than by observing that this particular run happened not to.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from app.coding import toolchain


# ---------------------------------------------------------------------------
# Bounded discovery
# ---------------------------------------------------------------------------

def test_discovery_never_walks_the_disk():
    """The same rule legacy_migration follows, for the same reason: a
    "diagnostic" that scans a drive is a disk scan with a friendly name."""
    source = Path(toolchain.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        getattr(node.func, "attr", getattr(node.func, "id", ""))
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    for forbidden in ("walk", "rglob", "iterdir", "glob"):
        assert forbidden not in called, f"toolchain must not search with {forbidden}()"


def test_nothing_is_installed_or_configured():
    """Structural, not behavioural: a test that merely observed this run
    installing nothing would pass against a module that installs
    something only on a machine the test never runs on."""
    source = Path(toolchain.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    for forbidden in ("npm install", "pip install", "winget", "choco ",
                      "setx", "reg add", "mkdir", "write_text"):
        assert forbidden not in lowered, f"toolchain must not {forbidden!r}"

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "run":
            # Every subprocess call must be a version probe and nothing else.
            assert any(
                kw.arg == "capture_output" for kw in node.keywords), (
                "a subprocess call in toolchain.py is not a captured version probe")


def test_every_probe_is_bounded():
    assert toolchain.PROBE_TIMEOUT_SECONDS <= 30
    source = Path(toolchain.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "run":
            assert any(kw.arg == "timeout" for kw in node.keywords), (
                "a version probe with no timeout can hang a diagnostic")


# ---------------------------------------------------------------------------
# What it reports
# ---------------------------------------------------------------------------

def test_the_report_covers_everything_the_brief_names():
    report = toolchain.diagnose()
    keys = {tool["key"] for tool in report["tools"]}
    assert {"git", "node", "npm", "pnpm", "yarn", "python", "pip"} <= keys


def test_each_tool_is_available_missing_unsupported_or_refused():
    for tool in toolchain.diagnose()["tools"]:
        assert tool["state"] in (toolchain.AVAILABLE, toolchain.MISSING,
                                 toolchain.UNSUPPORTED, toolchain.REFUSED)
        assert tool["display"]
        assert tool["depends"], "a tool must say what stops working without it"
        if tool["state"] == toolchain.AVAILABLE:
            assert tool["version"], "an available tool must report its version"
            assert tool["found_via"], "an available tool must say how it was found"
        else:
            assert tool["detail"], "an unusable tool must say why"


def test_no_report_carries_a_filesystem_path():
    """`pip --version` answers "pip 24.0 from C:\\Users\\<name>\\..." and
    this string goes on screen and into diagnostics."""
    report = toolchain.diagnose(Path.cwd())
    blob = str(report)
    assert "/usr/lib" not in blob
    assert "dist-packages" not in blob
    assert "C:\\Users" not in blob
    assert str(Path.home()) not in blob


@pytest.mark.parametrize("banner,expected", [
    ("pip 24.0 from /usr/lib/python3/dist-packages/pip (python 3.11)", "pip 24.0"),
    (r"pip 24.0 from C:\Users\someone\AppData\Roaming\pip (python 3.11)", "pip 24.0"),
    ("git version 2.43.0", "git version 2.43.0"),
    ("v22.22.2", "v22.22.2"),
])
def test_a_version_banner_is_stripped_of_paths(banner, expected):
    assert toolchain._without_paths(banner) == expected


def test_a_missing_tool_names_what_stops_working(monkeypatch):
    monkeypatch.setattr(toolchain.shutil, "which", lambda name: None)
    monkeypatch.setattr(toolchain, "_standard_locations", lambda name: [])

    report = toolchain.diagnose()
    assert report["missing"], "with nothing installed, something must be reported missing"
    assert report["cannot_run"], "and what stops working must be named"
    assert all(tool["state"] == toolchain.MISSING for tool in report["tools"])


# ---------------------------------------------------------------------------
# A project may not supply its own toolchain
# ---------------------------------------------------------------------------

def test_an_executable_inside_the_project_is_refused(tmp_path, monkeypatch):
    """The defect this prevents: a `git.exe` committed to a repository
    being the Git that inspects it."""
    project = tmp_path / "hostile-repo"
    project.mkdir()
    impostor = project / ("git.exe" if os.name == "nt" else "git")
    impostor.write_text("#!/bin/sh\necho pwned\n", encoding="utf-8")
    impostor.chmod(0o755)

    monkeypatch.setattr(toolchain.shutil, "which",
                        lambda name: str(impostor) if name == "git" else None)
    monkeypatch.setattr(toolchain, "_standard_locations", lambda name: [])

    spec = next(s for s in toolchain.TOOLS if s.key == "git")
    report = toolchain.probe(spec, project)

    assert report.state == toolchain.REFUSED
    assert "inside this project" in report.detail
    assert report.version == "", "a refused executable must never be run"


def test_an_executable_in_a_subfolder_of_the_project_is_also_refused(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    (project / "tools" / "bin").mkdir(parents=True)
    impostor = project / "tools" / "bin" / ("node.exe" if os.name == "nt" else "node")
    impostor.write_text("#!/bin/sh\n", encoding="utf-8")
    impostor.chmod(0o755)

    monkeypatch.setattr(toolchain.shutil, "which",
                        lambda name: str(impostor) if name == "node" else None)
    monkeypatch.setattr(toolchain, "_standard_locations", lambda name: [])

    spec = next(s for s in toolchain.TOOLS if s.key == "node")
    assert toolchain.probe(spec, project).state == toolchain.REFUSED


def test_a_sibling_folder_with_a_similar_name_is_not_confused(tmp_path, monkeypatch):
    """`/a/b-evil` is not inside `/a/b` — the same component-wise rule
    `workspace.py` enforces, applied to executables."""
    project = tmp_path / "b"
    project.mkdir()
    sibling = tmp_path / "b-evil"
    sibling.mkdir()
    real = sibling / ("git.exe" if os.name == "nt" else "git")
    real.write_text("#!/bin/sh\necho git version 2.0.0\n", encoding="utf-8")
    real.chmod(0o755)

    refusal = toolchain._impersonation_refusal(real, project)
    assert refusal == "", "a sibling folder is not inside the project"


def test_a_refusal_is_distinguishable_from_an_absence(tmp_path, monkeypatch):
    """"JARVIS refused the git.exe in your project" and "Git is not
    installed" call for entirely different responses."""
    project = tmp_path / "repo"
    project.mkdir()
    impostor = project / ("git.exe" if os.name == "nt" else "git")
    impostor.write_text("", encoding="utf-8")

    monkeypatch.setattr(toolchain, "_standard_locations", lambda name: [])
    monkeypatch.setattr(toolchain.shutil, "which",
                        lambda name: str(impostor) if name == "git" else None)
    spec = next(s for s in toolchain.TOOLS if s.key == "git")
    refused = toolchain.probe(spec, project)

    monkeypatch.setattr(toolchain.shutil, "which", lambda name: None)
    missing = toolchain.probe(spec, project)

    assert refused.state != missing.state
    assert refused.detail != missing.detail


def test_no_project_root_means_no_impersonation_check_is_possible():
    """Called without a project, there is nothing to compare against —
    and the module must not invent one."""
    assert toolchain._impersonation_refusal(Path("/usr/bin/git"), None) == ""


# ---------------------------------------------------------------------------
# The project's own declarations
# ---------------------------------------------------------------------------

def test_a_project_that_declares_no_test_runner_is_reported_as_such(tmp_path):
    from tests import coding_fixtures as fx

    root = fx.static_site(tmp_path / "plain", with_defect=False)
    rows = {row["intent"]: row for row in toolchain.project_tools(root)}
    assert set(rows) == {"format", "lint", "typecheck", "test", "build"}
    assert rows["test"]["declared"] is False
    assert "will not guess" in rows["test"]["detail"]


def test_a_declared_script_is_reported_with_its_command(tmp_path):
    from tests import coding_fixtures as fx

    root = fx.vite_react_ts(tmp_path / "app")
    rows = {row["intent"]: row for row in toolchain.project_tools(root)}
    declared = [intent for intent, row in rows.items() if row["declared"]]
    assert declared, "a Vite project declares scripts"
    for intent in declared:
        assert rows[intent]["command"], "a declared tool must name its command"


def test_a_project_virtual_environment_is_found_by_name_not_by_searching(tmp_path):
    root = tmp_path / "py"
    binary = root / (".venv/Scripts" if os.name == "nt" else ".venv/bin")
    binary.mkdir(parents=True)
    (binary / ("python.exe" if os.name == "nt" else "python")).write_text("", encoding="utf-8")

    rows = toolchain.virtual_environments(root)
    assert rows[0]["name"] == ".venv"
    assert rows[0]["state"] == toolchain.AVAILABLE


def test_no_virtual_environment_is_said_plainly(tmp_path):
    root = tmp_path / "py"
    root.mkdir()
    rows = toolchain.virtual_environments(root)
    assert rows[0]["state"] == toolchain.MISSING
    assert "system interpreter" in rows[0]["detail"]


# ---------------------------------------------------------------------------
# A missing tool is never a pass
# ---------------------------------------------------------------------------

def test_a_step_whose_tool_is_missing_is_not_reported_as_passed(monkeypatch):
    monkeypatch.setattr(toolchain.shutil, "which", lambda name: None)
    monkeypatch.setattr(toolchain, "_standard_locations", lambda name: [])

    reason = toolchain.blocked_reason("lint")
    assert reason, "a lint step with no Node must not simply succeed"
    assert "not a passing result" in reason


def test_a_step_whose_tools_are_present_is_not_blocked():
    if not toolchain.probe(next(s for s in toolchain.TOOLS if s.key == "node")).usable:
        pytest.skip("no Node.js on this machine")
    assert toolchain.blocked_reason("lint") == ""


def test_an_unsupported_version_is_its_own_state(monkeypatch, tmp_path):
    """"Too old" is not "missing": one is fixed by installing, the other
    by upgrading, and the message has to say which."""
    fake = tmp_path / "node"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(toolchain.shutil, "which",
                        lambda name: str(fake) if name == "node" else None)
    monkeypatch.setattr(toolchain, "_version_of", lambda path, args: ("v12.0.0", ""))

    spec = next(s for s in toolchain.TOOLS if s.key == "node")
    report = toolchain.probe(spec)
    assert report.state == toolchain.UNSUPPORTED
    assert "18" in report.detail
    assert report.version == "v12.0.0", "the version found is still reported"


def test_a_tool_that_cannot_answer_is_unsupported_not_missing(monkeypatch, tmp_path):
    fake = tmp_path / "git"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(toolchain.shutil, "which",
                        lambda name: str(fake) if name == "git" else None)
    monkeypatch.setattr(toolchain, "_version_of",
                        lambda path, args: ("", "It did not answer --version within 10 seconds."))

    spec = next(s for s in toolchain.TOOLS if s.key == "git")
    report = toolchain.probe(spec)
    assert report.state == toolchain.UNSUPPORTED
    assert "10 seconds" in report.detail
