"""Integration tests proving the v0.2 pipeline is actually wired end to
end through real commands — not just unit-tested in isolation.

Covers: dispatch creates a persisted action_lifecycle record and publishes
WebSocket events; the policy engine's decision (not a separate ad-hoc
check) drives auto-execute/require-approval/deny; the pending-action queue
and the lifecycle audit trail share one id and stay in sync through
approve/cancel; a blocked tool routed through dispatch is denied before
any handler runs; the new real tools (open_app, open_website,
read_clipboard) carry the risk/input_model contract for real.

Uses the real app (TestClient) exactly like test_approvals.py, so this
exercises the actual production wiring in app/core/router.py and
app/api/actions.py, not a reimplementation of it. Unlike test_approvals.py
(which mocks db.database.get_db away entirely, since it never needs to
inspect what was written), these tests need to read back what dispatch
persisted — so get_db() is patched to a real, isolated, temp-file-backed
Database for the duration of each test instead of a bare MagicMock, using
the same technique as test_action_lifecycle.py.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.core.events import EventType, event_bus
from app.core.models import ActionLifecycleStatus


@pytest.fixture(scope="module")
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield client


@pytest.fixture
def test_db(tmp_path):
    from db.database import Database
    from db.migrations import create_tables

    db_path = tmp_path / "pipeline_test.db"
    create_tables(db_path=db_path)
    return Database(db_path=db_path)


@pytest.fixture(autouse=True)
def _patch_get_db(test_db):
    """Route every get_db() call — from router.py, actions.py, and
    action_lifecycle.py alike — to one real, isolated temp database for
    this test, so what dispatch persists can actually be read back."""
    with patch("db.database.get_db", return_value=test_db):
        yield


@pytest.fixture(autouse=True)
def reset_pending_store():
    from app.core.pending_actions import pending_store
    with pending_store._lock:
        pending_store._actions.clear()
    yield
    with pending_store._lock:
        pending_store._actions.clear()


def _events_since(seq: int):
    return event_bus.since(seq)


# --- auto-execute (READ_ONLY / REVERSIBLE) path creates a real lifecycle record ---

def test_read_only_command_creates_succeeded_lifecycle_record(api_client):
    from app.core.action_lifecycle import list_recent

    r = api_client.post("/command", json={"command": "system status"})
    assert r.status_code == 200
    assert r.json()["success"] is True

    recent = list_recent(limit=5)
    match = next((rec for rec in recent if rec.tool_name == "system_status"), None)
    assert match is not None
    assert match.status == ActionLifecycleStatus.SUCCEEDED
    assert match.risk == "read_only"
    assert match.duration_ms is not None


def test_auto_execute_command_publishes_proposed_and_result_events(api_client):
    last_seq = event_bus.latest_seq()
    api_client.post("/command", json={"command": "system status"})

    new_events = _events_since(last_seq)
    types = [e.type for e in new_events]
    assert EventType.ACTION_PROPOSED in types
    assert EventType.ACTION_RESULT in types


# --- approval-required path: one id shared by pending_store and the lifecycle audit trail ---

def test_approval_required_command_creates_matching_lifecycle_record(api_client):
    from app.core.action_lifecycle import get as lifecycle_get

    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]

    record = lifecycle_get(action_id)
    assert record is not None
    assert record.status == ActionLifecycleStatus.PENDING_APPROVAL
    assert record.tool_name == "clear_logs"


def test_confirming_pending_action_advances_lifecycle_to_a_terminal_state(api_client):
    from app.core.action_lifecycle import get as lifecycle_get

    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]

    confirm = api_client.post(f"/actions/{action_id}/confirm")
    assert confirm.status_code == 200

    record = lifecycle_get(action_id)
    assert record is not None
    assert record.status in (ActionLifecycleStatus.SUCCEEDED, ActionLifecycleStatus.FAILED)
    assert record.duration_ms is not None


def test_cancelling_pending_action_marks_lifecycle_cancelled(api_client):
    from app.core.action_lifecycle import get as lifecycle_get

    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]

    cancel = api_client.post(f"/actions/{action_id}/cancel")
    assert cancel.status_code == 200

    record = lifecycle_get(action_id)
    assert record is not None
    assert record.status == ActionLifecycleStatus.CANCELLED


def test_confirming_already_cancelled_action_does_not_execute_twice(api_client):
    """The pre-existing double-execution guard (pending_store.confirm
    refuses a non-pending action) still governs execution; the lifecycle
    mirror must not contradict it by silently re-opening a cancelled
    record."""
    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]

    api_client.post(f"/actions/{action_id}/cancel")
    second_confirm = api_client.post(f"/actions/{action_id}/confirm")

    assert second_confirm.json()["success"] is False


# --- read_clipboard: SENSITIVE, always approval-required ---

def test_read_clipboard_command_never_auto_executes(api_client):
    r = api_client.post("/command", json={"command": "read clipboard"})
    body = r.json()
    assert body["requires_approval"] is True
    assert body["pending_action_id"] is not None


def test_read_clipboard_tool_declares_sensitive_risk():
    from app.core.tool_registry import registry
    from app.core.brain import brain

    brain.initialise()
    tool = registry.get("read_clipboard")
    assert tool is not None
    assert tool.definition.risk.value == "sensitive"
    assert tool.definition.permission_level.value == "approval_required"


def test_confirmed_clipboard_action_never_leaks_content_into_ws_events(api_client):
    """The clipboard content itself is delivered to the approving caller's
    direct HTTP response (by design — that's the point of approving the
    read) but must never appear in any WebSocket event, since those are a
    broadcast channel, not a private response to one caller."""
    import sys
    import types

    secret = "super-secret-clipboard-xyz"
    fake_root = MagicMock()
    fake_root.clipboard_get.return_value = secret
    fake_tkinter = types.ModuleType("tkinter")
    fake_tkinter.Tk = MagicMock(return_value=fake_root)
    fake_tkinter.TclError = type("TclError", (Exception,), {})

    with patch.dict(sys.modules, {"tkinter": fake_tkinter}):
        r = api_client.post("/command", json={"command": "read clipboard"})
        action_id = r.json()["pending_action_id"]

        last_seq = event_bus.latest_seq()
        confirm = api_client.post(f"/actions/{action_id}/confirm")

    assert confirm.json()["data"]["content"] == secret  # delivered to the caller who approved it

    new_events = _events_since(last_seq)
    assert len(new_events) > 0
    for evt in new_events:
        assert secret not in str(evt.payload)


# --- open_app / open_website carry the v0.2 typed contract ---

def test_open_app_declares_reversible_risk_and_input_model():
    from app.core.tool_registry import registry
    from app.core.brain import brain
    from app.desktop.apps import OpenAppInput

    brain.initialise()
    tool = registry.get("open_app")
    assert tool.definition.risk.value == "reversible"
    assert tool.definition.input_model is OpenAppInput


def test_open_website_declares_reversible_risk_and_input_model():
    from app.core.tool_registry import registry
    from app.core.brain import brain
    from app.desktop.web import OpenWebsiteInput

    brain.initialise()
    tool = registry.get("open_website")
    assert tool.definition.risk.value == "reversible"
    assert tool.definition.input_model is OpenWebsiteInput


# --- GET /tools survives serializing a real input_model (regression: this
# used to crash with PydanticSerializationError once a real tool declared
# a Pydantic class for input_model) ---

def test_tools_endpoint_serializes_input_model_as_json_schema(api_client):
    r = api_client.get("/tools")
    assert r.status_code == 200
    tools = {t["name"]: t for t in r.json()}

    assert tools["open_app"]["input_model"]["properties"]["app_name"]["type"] == "string"
    assert tools["system_status"]["input_model"] is None
    assert tools["read_clipboard"]["risk"] == "sensitive"


# --- policy DENY path at the router level (previously unreachable/untested) ---

def _make_blocked_router(command_pattern: str):
    """A CommandRouter wired with one BLOCKED tool reachable via
    *command_pattern*, isolated from the real ROUTES/registry."""
    from app.core.models import PermissionLevel, ToolCategory, ToolDefinition
    from app.core.router import ROUTES, CommandRouter, Route
    from app.core.tool_registry import ToolRegistry

    handler = MagicMock(return_value={"success": True, "message": "SHOULD NOT RUN", "data": None})
    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name="steal_password",
            description="blocked test tool",
            permission_level=PermissionLevel.BLOCKED,
            category=ToolCategory.UTILITY,
        ),
        handler,
    )
    router = CommandRouter(reg)
    extended_routes = list(ROUTES) + [Route(command_pattern, "steal_password")]
    return router, extended_routes, handler


def test_dispatch_denies_blocked_tool_before_any_handler_runs():
    router, extended_routes, handler = _make_blocked_router(r"^do\s+the\s+blocked\s+thing$")

    with patch("app.core.router.ROUTES", extended_routes):
        resp = router.route("do the blocked thing")

    assert resp.success is False
    assert "blocked" in resp.message.lower()
    handler.assert_not_called()


def test_dispatch_denied_tool_recorded_as_blocked_in_lifecycle():
    from app.core.action_lifecycle import list_recent

    router, extended_routes, _handler = _make_blocked_router(r"^do\s+the\s+blocked\s+thing\s+2$")

    with patch("app.core.router.ROUTES", extended_routes):
        router.route("do the blocked thing 2")

    recent = list_recent(limit=5)
    match = next((rec for rec in recent if rec.tool_name == "steal_password"), None)
    assert match is not None
    assert match.status == ActionLifecycleStatus.BLOCKED
