"""Regression checks for the repository-local Claude toolkit adoption.

These checks validate committed configuration only. They do not claim an external
marketplace loaded in a particular cloud session, or that plugin hooks executed.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CLAUDE = ROOT / ".claude"
TOOLKIT_COMMIT = "81796b8d5d35bef723fc180f4dd98c61f90e2052"

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


def test_settings_enable_only_the_three_reviewed_toolkit_plugins():
    settings = json.loads((CLAUDE / "settings.json").read_text(encoding="utf-8"))

    assert settings["extraKnownMarketplaces"]["dado-tools"]["source"] == {
        "source": "github",
        "repo": "Dado211207/dado-claude-toolkit",
    }
    assert settings["enabledPlugins"] == {
        "dado-core@dado-tools": True,
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
    provenance = (CLAUDE / "TOOLKIT.md").read_text(encoding="utf-8")
    state = (ROOT / "docs" / "ai" / "PROJECT_STATE.md").read_text(encoding="utf-8")

    assert TOOLKIT_COMMIT in provenance
    assert "not authorization" in " ".join(state.split())
    assert "Draft, open, unmerged" in state
    assert "Real microphone" in state
    assert "No tag, release, signing, merge or deployment" in state


def test_claude_instructions_route_non_trivial_work_through_project_state():
    instructions = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert "docs/ai/PROJECT_STATE.md" in instructions
    assert "/orient" in instructions
    assert "Existing JARVIS rules take precedence" in instructions
