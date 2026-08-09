"""Standing security invariants, checked against the real application.

These are not tests of a feature. They are the properties that must stay
true as features get added, expressed so that adding an endpoint or a
subprocess call that breaks one fails the build rather than waiting to
be noticed in review. Each one corresponds to a rule in CLAUDE.md or
docs/THREAT_MODEL.md; the comment on each says which promise it defends.

Deliberately written against the assembled app and the source tree
rather than a list someone maintains by hand — a checklist that has to
be updated alongside the thing it checks stops being a check.
"""

import ast
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from app.api.server import app

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
TEMPLATES_DIR = APP_DIR / "ui" / "templates"

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _api_routes():
    """Every APIRoute in the app, including those inside included routers.

    This FastAPI version does not flatten `include_router()` results into
    `app.routes`; it inserts an `_IncludedRouter` wrapper that holds the
    real router on `.original_router`. A walk that only iterates
    `app.routes` finds the four docs routes and nothing else — which is
    why test_the_route_walk_actually_finds_the_api exists below, guarding
    every endpoint assertion in this file against passing vacuously.
    """
    found = []
    seen = set()

    def walk(routes):
        for route in routes:
            if id(route) in seen:
                continue
            seen.add(id(route))
            if isinstance(route, APIRoute):
                found.append(route)
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(getattr(inner, "routes", []))
            nested = getattr(route, "routes", None)
            if nested:
                walk(nested)

    walk(app.routes)
    return found


def _requires_session_token(route) -> bool:
    return any(
        getattr(getattr(dep, "call", None), "__name__", "") == "require_session_token"
        for dep in route.dependant.dependencies
    )


def _python_sources():
    return sorted(APP_DIR.rglob("*.py")) + sorted((REPO_ROOT / "db").rglob("*.py"))


def _non_docstring_strings(path: Path):
    """Every string constant in a module that is not a docstring, with
    its line number. Comments and docstrings necessarily discuss the
    things these tests forbid, and a raw substring search cannot tell an
    explanation apart from the real thing."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                docstrings.add(id(first.value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.value, node.lineno


# ---------------------------------------------------------------------------
# The app really was assembled — guards every test below
# ---------------------------------------------------------------------------

def test_the_route_walk_actually_finds_the_api():
    """Without this, a walk that returns nothing would make every
    endpoint test below pass vacuously."""
    paths = {route.path for route in _api_routes()}

    assert "/command" in paths
    assert len(paths) > 25


# ---------------------------------------------------------------------------
# Mutations are protected
# ---------------------------------------------------------------------------

def test_every_mutating_endpoint_requires_the_session_token():
    """CLAUDE.md's v0.2 rule. A new POST added without the dependency is
    reachable from any page the user happens to have open, so this fails
    the build rather than waiting for review to catch it."""
    unprotected = [
        f"{method} {route.path}"
        for route in _api_routes()
        for method in sorted(route.methods & MUTATING_METHODS)
        if not _requires_session_token(route)
    ]

    assert unprotected == [], f"unprotected mutating endpoint(s): {unprotected}"


def test_read_only_endpoints_do_not_demand_a_token_unnecessarily():
    """The other half of the same judgement: a GET that mutates nothing
    should not require a token, or the requirement stops meaning
    anything. Stated as an inventory so a GET that starts mutating has to
    be argued for here."""
    token_gated_gets = sorted(
        route.path for route in _api_routes()
        if "GET" in route.methods and _requires_session_token(route)
    )

    assert token_gated_gets == []


# ---------------------------------------------------------------------------
# The API stays local
# ---------------------------------------------------------------------------

def test_the_api_binds_to_loopback_by_default():
    from app.config import Settings

    assert Settings().jarvis_host == "127.0.0.1"


def test_nothing_binds_to_all_interfaces():
    """CLAUDE.md: never 0.0.0.0 without an explicit security review."""
    offenders = [
        f"{path.name}:{line}"
        for path in _python_sources()
        for value, line in _non_docstring_strings(path)
        if "0.0.0.0" in value
    ]

    assert offenders == [], f"a bind-all address appears in {offenders}"


def test_the_websocket_stream_cannot_run_a_command():
    """CLAUDE.md: /ws/events is read-only. It broadcasts; it must never
    accept a command or an approval."""
    source = (APP_DIR / "api" / "ws.py").read_text(encoding="utf-8")

    for forbidden in ("brain.process", "registry.execute", "execute_approved", "pending_store.confirm"):
        assert forbidden not in source, f"the event stream reaches {forbidden}"


# ---------------------------------------------------------------------------
# No dangerous execution
# ---------------------------------------------------------------------------

def test_no_subprocess_call_uses_a_shell():
    """CLAUDE.md: subprocess calls use explicit argument lists. shell=True
    turns any string that reaches it into a command line."""
    offenders = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "shell" and not (
                    isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                ):
                    offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], f"shell=True (or a non-literal shell argument) at {offenders}"


@pytest.mark.parametrize("name", ["eval", "exec", "compile", "__import__"])
def test_no_dynamic_code_execution(name):
    """There is no legitimate reason for this application to build code
    at runtime, and every one of these turns a data path into a code
    path."""
    offenders = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name:
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], f"{name}() called at {offenders}"


def test_nothing_deserialises_untrusted_pickles():
    """pickle.loads on anything a user can influence is remote code
    execution. The launcher's IPC uses multiprocessing.connection with an
    HMAC authkey precisely so an unauthenticated peer never gets that
    far."""
    offenders = []
    for path in _python_sources():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("loads", "load"):
                if isinstance(node.value, ast.Name) and node.value.id in ("pickle", "cPickle", "marshal"):
                    offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], f"pickle/marshal deserialisation at {offenders}"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def test_no_api_key_literal_is_committed():
    """CLAUDE.md: credentials live in .env or the OS credential store,
    never in source. Checked over real string constants so the many
    modules that discuss "sk-" prefixes do not trip it."""
    offenders = []
    for path in _python_sources():
        for value, line in _non_docstring_strings(path):
            stripped = value.strip()
            # A real Anthropic key is far longer than any placeholder or
            # prefix constant this codebase legitimately contains.
            if stripped.startswith("sk-") and len(stripped) > 25:
                offenders.append(f"{path.name}:{line}")

    assert offenders == [], f"a credential-shaped literal appears at {offenders}"


def test_no_template_renders_a_credential():
    """CLAUDE.md's Phase 4 rule: templates never render the API key."""
    offenders = []
    for path in sorted(TEMPLATES_DIR.glob("*.html")):
        content = path.read_text(encoding="utf-8")
        for needle in ("ANTHROPIC_API_KEY", "effective_api_key", "anthropic_api_key"):
            if needle in content:
                offenders.append(f"{path.name}: {needle}")

    assert offenders == [], f"a credential reference in {offenders}"


def test_no_template_disables_autoescaping():
    """Jinja2's |safe on anything derived from user input reintroduces
    exactly the XSS the textContent rule exists to prevent."""
    offenders = [
        path.name for path in sorted(TEMPLATES_DIR.glob("*.html"))
        if "|safe" in path.read_text(encoding="utf-8") or "| safe" in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], f"autoescaping is bypassed in {offenders}"


# ---------------------------------------------------------------------------
# The approval gate
# ---------------------------------------------------------------------------

def test_no_approval_required_tool_can_be_executed_directly():
    """CLAUDE.md's Phase 5 rule, checked against the real registry with
    every real tool registered: registry.execute() must refuse, and only
    execute_approved() may run one."""
    from app.core.brain import brain
    from app.core.models import PermissionLevel
    from app.core.tool_registry import registry

    brain.initialise()
    gated = [
        tool.definition.name for tool in registry.list_tools()
        if tool.definition.permission_level == PermissionLevel.APPROVAL_REQUIRED
    ]

    assert gated, "no approval-required tool is registered — this test would prove nothing"
    for name in gated:
        result = registry.execute(name)
        assert result["success"] is False, f"{name} executed without approval"


def test_no_tool_is_registered_that_this_project_forbids():
    """CLAUDE.md's permanent exclusion list, asserted against what is
    actually registered rather than trusted to reviewers."""
    from app.core.brain import brain
    from app.core.tool_registry import registry

    brain.initialise()
    names = {tool.definition.name for tool in registry.list_tools()}

    forbidden_fragments = (
        "password", "keylog", "webcam", "camera", "record_screen", "port_scan",
        "network_scan", "remote_control", "anydesk", "send_email", "delete_files",
        "shutdown", "restart_computer", "sign_out",
    )
    offenders = [name for name in names for fragment in forbidden_fragments if fragment in name]

    assert offenders == [], f"a forbidden capability is registered: {offenders}"


def test_the_clipboard_tool_is_permanently_approval_required():
    """CLAUDE.md's v0.2 rule, stated as "permanently" — so it gets a test
    rather than a comment."""
    from app.core.brain import brain
    from app.core.models import PermissionLevel, RiskLevel
    from app.core.tool_registry import registry

    brain.initialise()
    definition = registry.get("read_clipboard").definition

    assert definition.permission_level == PermissionLevel.APPROVAL_REQUIRED
    assert definition.risk == RiskLevel.SENSITIVE


def test_there_is_no_clipboard_writing_or_monitoring():
    """The same rule's other half: read_clipboard is the only clipboard
    capability, and it must never grow into the clipboard sniffer the
    Safety rules forbid."""
    from app.core.brain import brain
    from app.core.tool_registry import registry

    brain.initialise()
    clipboard_tools = {
        tool.definition.name for tool in registry.list_tools()
        if "clipboard" in tool.definition.name
    }

    assert clipboard_tools == {"read_clipboard"}


# ---------------------------------------------------------------------------
# Errors and logs
# ---------------------------------------------------------------------------

def test_no_endpoint_returns_raw_exception_text():
    """docs/THREAT_MODEL.md: raw exception text can carry paths, SDK
    internals and request fragments. Every handler must go through
    to_safe_error() instead of str(exc)."""
    offenders = []
    for path in sorted((APP_DIR / "api").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # str(exc) / f"...{exc}" inside an except block, returned to a client
            if isinstance(node, ast.ExceptHandler) and node.name:
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) \
                            and inner.func.id == "str" and inner.args:
                        arg = inner.args[0]
                        if isinstance(arg, ast.Name) and arg.id == node.name:
                            offenders.append(f"{path.name}:{inner.lineno}")

    assert offenders == [], f"raw exception text may reach a response at {offenders}"


def test_tool_inputs_are_redacted_before_they_are_persisted():
    """CLAUDE.md's v0.2 rule. Proven through the real audit trail rather
    than by reading redaction.py, since the rule is about the call site."""
    from app.core.action_lifecycle import propose

    record = propose("a_tool", {"api_key": "sk-must-not-be-stored", "token": "secret", "plain": "fine"})

    assert "sk-must-not-be-stored" not in str(record.input_summary)
    assert "secret" not in str(record.input_summary)
    assert record.input_summary["plain"] == "fine"
