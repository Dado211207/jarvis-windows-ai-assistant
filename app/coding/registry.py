"""The Coding Workspace capability registry — separate, on purpose.

This is **not** `app.core.tool_registry.registry`, and nothing here is
ever added to it. That separation is the mechanism behind a promise made
in docs/coding-workspace-architecture.md §2.2: the ordinary chat
assistant does not gain filesystem or shell powers because this feature
exists.

Concretely, for a coding capability to be reachable from chat, somebody
would have to (a) add it to the global registry *and* (b) add a route to
`app/core/router.py`. Neither exists, and
`tests/test_coding_isolation.py` asserts both — the first by enumerating
the global registry after `brain.initialise()`, the second by running
every capability name through `router.find_route()` and requiring None.

The entries here are the declared contract: what each capability is, what
risk it carries, and whether it can ever run without asking. The UI reads
this to render the permission matrix, so what the user is shown comes
from the same source the code uses rather than a hand-maintained copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from app.core.models import RiskLevel


@dataclass(frozen=True)
class CodingCapability:
    name: str
    description: str
    risk: RiskLevel
    always_requires_approval: bool
    reachable_from_chat: bool = False   # never True; present so the test can assert it

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk.value,
            "always_requires_approval": self.always_requires_approval,
            "reachable_from_chat": self.reachable_from_chat,
        }


_CAPABILITIES: Dict[str, CodingCapability] = {}


def _declare(capability: CodingCapability) -> None:
    _CAPABILITIES[capability.name] = capability


_declare(CodingCapability(
    "inspect_file", "Read one text file inside the project. Protected files are "
    "reported but never read.", RiskLevel.READ_ONLY, False))
_declare(CodingCapability(
    "list_files", "List the project's files.", RiskLevel.READ_ONLY, False))
_declare(CodingCapability(
    "search_text", "Search project files for a string.", RiskLevel.READ_ONLY, False))
_declare(CodingCapability(
    "git_inspect", "Read Git status, diff, log or branch.", RiskLevel.READ_ONLY, False))
_declare(CodingCapability(
    "propose_patch", "Rewrite a text file, shown as a diff first and refused if the "
    "file changed underneath.", RiskLevel.REVERSIBLE, False))
_declare(CodingCapability(
    "create_file", "Create a new text file inside the project.", RiskLevel.REVERSIBLE, False))
_declare(CodingCapability(
    "rename_file", "Rename a file inside the project.", RiskLevel.REVERSIBLE, False))
_declare(CodingCapability(
    "delete_file", "Delete a file. Always needs approval, every time.",
    RiskLevel.SENSITIVE, True))
_declare(CodingCapability(
    "run_command", "Run a development command as an argument list. The risk tier "
    "depends on the command — see app/coding/commands.py.",
    RiskLevel.SENSITIVE, False))
_declare(CodingCapability(
    "start_preview", "Start the project's own dev server, bound to 127.0.0.1.",
    RiskLevel.REVERSIBLE, False))
_declare(CodingCapability(
    "stop_preview", "Stop the preview JARVIS started.", RiskLevel.READ_ONLY, False))
_declare(CodingCapability(
    "browser_check", "Check a route on the owned loopback preview.",
    RiskLevel.READ_ONLY, False))
_declare(CodingCapability(
    "request_approval", "Ask the user a question.", RiskLevel.READ_ONLY, False))
_declare(CodingCapability(
    "finish_task", "End the task with a summary.", RiskLevel.READ_ONLY, False))


def capabilities() -> List[CodingCapability]:
    return sorted(_CAPABILITIES.values(), key=lambda c: c.name)


def get(name: str) -> Optional[CodingCapability]:
    return _CAPABILITIES.get(name)


def names() -> List[str]:
    return sorted(_CAPABILITIES)


def as_matrix() -> List[dict]:
    return [c.as_dict() for c in capabilities()]
