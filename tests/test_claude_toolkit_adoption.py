"""Regression checks for the repository-local Claude toolkit adoption.

These checks validate committed configuration only. They do not claim an external
marketplace loaded in a particular cloud session, or that plugin hooks executed.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CLAUDE = ROOT / ".claude"
TOOLKIT_COMMIT = "da8276e03b54d521462a0a15861d58227fb1546a"
UI_UPSTREAM_COMMIT = "8bd29e775453ebcae52b6e6514fbf134df0c5770"

EXPECTED_SKILLS = {
    "artifact-integrity",
    "ci-evidence",
    "debug-evidence",
    "deploy-gate",
    "draft-pr",
    "install-lifecycle-test",
    "orient",
    "packaging-verify",
    "plan-change",
    "pre-change-snapshot",
    "project-state",
    "python-app-review",
    "runtime-and-devices",
    "safe-edit",
    "select-tests",
    "uncertainty-log",
    "ui-ux-pro-max",
    "verify-and-report",
    "windows-ci-portability",
}

EXPECTED_AGENTS = {
    "ci-evidence-auditor",
    "final-verifier",
    "implementation-reviewer",
    "packaging-verifier",
    "python-windows-reviewer",
    "release-gatekeeper",
    "repository-auditor",
    "security-reviewer",
    "test-investigator",
}


def test_settings_enable_only_the_four_reviewed_toolkit_plugins():
    settings = json.loads((CLAUDE / "settings.json").read_text(encoding="utf-8"))

    assert settings["extraKnownMarketplaces"]["dado-tools"]["source"] == {
        "source": "github",
        "repo": "Dado211207/dado-claude-toolkit",
    }
    assert settings["enabledPlugins"] == {
        "dado-core@dado-tools": True,
        "dado-ui-design@dado-tools": True,
        "dado-python-windows@dado-tools": True,
        "dado-release-safety@dado-tools": True,
    }
    assert "allow" not in settings["permissions"]


def test_repository_fallback_contains_the_reviewed_skill_and_agent_set():
    skills = {path.parent.name for path in (CLAUDE / "skills").glob("*/SKILL.md")}
    agents = {path.stem for path in (CLAUDE / "agents").glob("*.md")}

    assert skills == EXPECTED_SKILLS
    assert agents == EXPECTED_AGENTS


def test_vendored_skill_links_use_repository_names_not_plugin_namespaces():
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((CLAUDE / "skills").glob("*/SKILL.md"))
    )

    assert "/dado-core:" not in combined
    assert "/dado-python-windows:" not in combined
    assert "/dado-release-safety:" not in combined


def test_provenance_and_project_state_are_explicit_about_the_limits():
    """PROJECT_STATE.md must name the state of its own work, not imply it.

    This previously asserted the literal string ``"Draft, open, unmerged"``.
    That pinned a *transient* status: the moment PR #17 was squash-merged the
    only way to keep this test green was to leave the file describing a merged
    pull request as an open draft — the test compelled the staleness it was
    meant to guard against. The durable property is asserted instead: an
    explicit ``State:`` line, and no pull request named without saying where
    it stands.
    """
    import re

    provenance = (CLAUDE / "TOOLKIT.md").read_text(encoding="utf-8")
    state = (ROOT / "docs" / "ai" / "PROJECT_STATE.md").read_text(encoding="utf-8")

    assert TOOLKIT_COMMIT in provenance
    assert UI_UPSTREAM_COMMIT in provenance
    assert "not authorization" in " ".join(state.split())
    assert "Real microphone" in state
    assert "No tag, release, signing, merge or deployment" in state

    assert re.search(r"^- State: \S", state, re.MULTILINE), (
        "the Active work section must carry an explicit `State:` line"
    )

    for line in state.splitlines():
        if re.search(r"\b(?:[Pp]ull request|PR) #\d+", line):
            assert re.search(r"merged|open|closed|[Dd]raft", line), (
                f"a pull request is named without its state: {line.strip()!r}"
            )


def test_ui_design_fallback_is_complete_local_and_dependency_free():
    skill = CLAUDE / "skills" / "ui-ux-pro-max"
    instructions = (skill / "SKILL.md").read_text(encoding="utf-8")

    assert "${CLAUDE_PLUGIN_ROOT}" not in instructions
    assert ".claude/skills/ui-ux-pro-max/scripts/search.py" in instructions
    assert (skill / "THIRD_PARTY_LICENSE.txt").is_file()
    assert (skill / "data" / "catalog-summary.json").is_file()
    assert (skill / "scripts" / "search.py").is_file()

    forbidden_modules = {"subprocess", "requests", "httpx", "urllib3", "socket"}
    for path in sorted((skill / "scripts").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            (node.module or "").split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        assert not imports & forbidden_modules

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "urllib.request"


def test_claude_instructions_route_non_trivial_work_through_project_state():
    instructions = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert "docs/ai/PROJECT_STATE.md" in instructions
    assert "/orient" in instructions
    assert "Existing JARVIS rules take precedence" in instructions
