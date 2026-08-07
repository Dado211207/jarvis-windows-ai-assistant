"""Tests for app/core/policy.py — risk classification and the policy engine.

The policy engine, not the router or the LLM, is the thing under test here:
given a risk level, does it return the right action, every time, with no
tool-specific special-casing hidden inside it (the one exception —
BLOCKED's reason string includes the tool name for a clearer message — is
tested explicitly so it doesn't silently grow into real special-casing).
"""

import pytest
from pydantic import BaseModel

from app.core.models import PermissionLevel, RiskLevel
from app.core.policy import PolicyAction, evaluate, risk_for


# --- risk_for(): legacy PermissionLevel -> RiskLevel mapping ---

def test_risk_for_uses_declared_risk_when_present():
    assert risk_for(PermissionLevel.SAFE, RiskLevel.READ_ONLY) == RiskLevel.READ_ONLY


def test_risk_for_falls_back_to_legacy_mapping_when_undeclared():
    assert risk_for(PermissionLevel.SAFE, None) == RiskLevel.REVERSIBLE
    assert risk_for(PermissionLevel.APPROVAL_REQUIRED, None) == RiskLevel.SENSITIVE
    assert risk_for(PermissionLevel.BLOCKED, None) == RiskLevel.BLOCKED


@pytest.mark.parametrize("level", list(PermissionLevel))
def test_risk_for_always_returns_something_for_every_legacy_level(level):
    """Every tool registered before v0.2 must get a risk classification —
    none of the three legacy levels may be unmapped."""
    result = risk_for(level, None)
    assert isinstance(result, RiskLevel)


# --- evaluate(): the policy matrix itself ---

def test_read_only_auto_executes():
    result = evaluate(RiskLevel.READ_ONLY, "system_status")
    assert result.action == PolicyAction.AUTO_EXECUTE
    assert result.risk == RiskLevel.READ_ONLY
    assert result.reason  # never empty — a decision without a reason is a bug


def test_reversible_auto_executes():
    result = evaluate(RiskLevel.REVERSIBLE, "open_app")
    assert result.action == PolicyAction.AUTO_EXECUTE


def test_sensitive_requires_approval():
    result = evaluate(RiskLevel.SENSITIVE, "read_clipboard")
    assert result.action == PolicyAction.REQUIRE_APPROVAL


def test_destructive_is_denied_not_silently_approved():
    """This milestone ships no destructive tool and no double-confirmation
    UI — DESTRUCTIVE must never resolve to a single-approval auto-pass."""
    result = evaluate(RiskLevel.DESTRUCTIVE, "hypothetical_delete_tool")
    assert result.action == PolicyAction.DENY
    assert result.action != PolicyAction.AUTO_EXECUTE


def test_blocked_is_always_denied():
    result = evaluate(RiskLevel.BLOCKED, "steal_password")
    assert result.action == PolicyAction.DENY


def test_blocked_reason_names_the_tool():
    result = evaluate(RiskLevel.BLOCKED, "steal_password")
    assert "steal_password" in result.reason


@pytest.mark.parametrize("risk,expected_action", [
    (RiskLevel.READ_ONLY, PolicyAction.AUTO_EXECUTE),
    (RiskLevel.REVERSIBLE, PolicyAction.AUTO_EXECUTE),
    (RiskLevel.SENSITIVE, PolicyAction.REQUIRE_APPROVAL),
    (RiskLevel.DESTRUCTIVE, PolicyAction.DENY),
    (RiskLevel.BLOCKED, PolicyAction.DENY),
])
def test_full_policy_matrix(risk, expected_action):
    """The complete, explicit matrix — one row per risk tier, so a future
    change to any single tier's behavior shows up as exactly one failing
    row here, not a surprise somewhere else."""
    assert evaluate(risk, "some_tool").action == expected_action


def test_policy_decision_is_deterministic_for_same_input():
    """No hidden state, no randomness — same risk in, same decision out,
    every time. The policy engine, not the LLM, makes the call."""
    results = [evaluate(RiskLevel.SENSITIVE, "read_clipboard").action for _ in range(20)]
    assert len(set(results)) == 1


# --- ToolDefinition's new optional fields (app/core/models.py) ---

def test_tool_definition_backward_compatible_without_new_fields():
    from app.core.models import ToolCategory, ToolDefinition
    definition = ToolDefinition(
        name="legacy_tool",
        description="A tool defined the old way, with none of the new fields.",
        permission_level=PermissionLevel.SAFE,
        category=ToolCategory.UTILITY,
    )
    assert definition.risk is None
    assert definition.timeout_seconds == 10.0
    assert definition.reversible is True
    assert definition.supports_preview is False
    assert definition.platform == ["any"]


def test_tool_definition_accepts_new_fields():
    from app.core.models import ToolCategory, ToolDefinition

    class DummyInput(BaseModel):
        value: str

    definition = ToolDefinition(
        name="new_tool",
        description="A tool declaring the full v0.2 contract.",
        permission_level=PermissionLevel.APPROVAL_REQUIRED,
        category=ToolCategory.SYSTEM,
        risk=RiskLevel.SENSITIVE,
        input_model=DummyInput,
        timeout_seconds=5.0,
        reversible=False,
        supports_preview=True,
        verification_strategy="Confirm the tool's own success flag.",
        platform=["windows"],
    )
    assert definition.risk == RiskLevel.SENSITIVE
    assert definition.input_model is DummyInput
    assert definition.reversible is False
    assert definition.platform == ["windows"]
