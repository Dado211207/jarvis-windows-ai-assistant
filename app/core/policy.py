"""Risk classification and the policy engine — the ONE place that decides
whether a proposed tool call auto-executes, needs approval, or is denied.

This is deliberately separate from app/core/permissions.py's existing
SAFE/APPROVAL_REQUIRED/BLOCKED enforcement, which keeps working exactly as
it does today for every tool already registered on `main`. `risk_for()`
below bridges the two: a tool that doesn't declare a RiskLevel explicitly
gets one derived from its existing PermissionLevel, so nothing already
registered has to change to keep working, while new tools get the richer,
five-tier model this milestone asks for.

The LLM is never consulted here and never in the call path that reaches
this module — see app/core/brain.py, which only ever returns plain text.
This module is the actual decision-maker for every real risk classification
that governs whether a tool can run.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel

from app.core.models import PermissionLevel, RiskLevel


class PolicyAction(str, Enum):
    AUTO_EXECUTE = "auto_execute"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PolicyResult(BaseModel):
    action: PolicyAction
    risk: RiskLevel
    reason: str


# Legacy PermissionLevel -> RiskLevel, used only when a tool does not
# declare risk explicitly (every tool registered before this milestone).
# This is intentionally conservative: SAFE maps to REVERSIBLE, not
# READ_ONLY, because most existing SAFE tools (open_app, create_note, ...)
# do have a side effect, even if a small/reversible one — nothing currently
# registered actually qualifies as "provably no side effect" except a
# handful of read-only status commands, and getting that distinction right
# requires a tool to opt in explicitly, not be guessed from three levels of
# legacy data.
_LEGACY_RISK_MAP = {
    PermissionLevel.SAFE: RiskLevel.REVERSIBLE,
    PermissionLevel.APPROVAL_REQUIRED: RiskLevel.SENSITIVE,
    PermissionLevel.BLOCKED: RiskLevel.BLOCKED,
}


def risk_for(permission_level: PermissionLevel, declared_risk: Optional[RiskLevel]) -> RiskLevel:
    """The risk a tool is actually evaluated at: its own declared RiskLevel
    if it has one, otherwise the conservative legacy mapping."""
    if declared_risk is not None:
        return declared_risk
    return _LEGACY_RISK_MAP[permission_level]


def evaluate(risk: RiskLevel, tool_name: str) -> PolicyResult:
    """The policy matrix. This function, not the caller, decides — callers
    (the router, the actions API) act on the PolicyResult, they don't
    re-derive it."""
    if risk == RiskLevel.READ_ONLY:
        return PolicyResult(
            action=PolicyAction.AUTO_EXECUTE,
            risk=risk,
            reason="Read-only: no persistent side effect.",
        )
    if risk == RiskLevel.REVERSIBLE:
        return PolicyResult(
            action=PolicyAction.AUTO_EXECUTE,
            risk=risk,
            reason="Reversible: limited, easily-undone side effect.",
        )
    if risk == RiskLevel.SENSITIVE:
        return PolicyResult(
            action=PolicyAction.REQUIRE_APPROVAL,
            risk=risk,
            reason="Sensitive: may reveal private information or interact with the desktop.",
        )
    if risk == RiskLevel.DESTRUCTIVE:
        # No destructive tool is registered in this milestone (see
        # CLAUDE.md's blocked-tools list and docs/THREAT_MODEL.md) — this
        # branch exists so the policy shape is correct and tested even
        # though nothing exercises it via a real tool yet. Approval alone
        # is not considered sufficient for DESTRUCTIVE by this milestone's
        # own spec ("double confirmation or blocked"); until a real
        # double-confirmation UI flow exists, DESTRUCTIVE denies outright
        # rather than approving on a single confirmation it doesn't yet have.
        return PolicyResult(
            action=PolicyAction.DENY,
            risk=risk,
            reason="Destructive: no double-confirmation flow exists yet in this milestone; denied rather than under-protected.",
        )
    # RiskLevel.BLOCKED
    return PolicyResult(
        action=PolicyAction.DENY,
        risk=risk,
        reason=f"Tool '{tool_name}' is permanently blocked in this milestone.",
    )
