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

from tests.conftest import credential_present

from app.core.events import EventType, event_bus
from app.core.models import ActionLifecycleStatus


@pytest.fixture(scope="module")
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield prime_session(client)


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


# --- exception-leak regression: full REST round trip, not just the unit level ---

def test_command_endpoint_never_leaks_raw_provider_exception_text(api_client):
    """End-to-end proof for the release-gate exception-leak fix: even
    through the real POST /command HTTP response body, a raw Anthropic
    SDK exception (which can carry request/response detail) never
    appears — only a safe category, message, and correlation ID."""
    sensitive_detail = (
        "Authorization: Bearer sk-ant-api03-TOTALLYREALSECRET "
        "at /home/realuser/.env line 3 — request to internal-host-7.corp"
    )

    with credential_present(), \
         patch("app.core.brain.settings") as mock_settings, \
         patch("anthropic.Anthropic") as mock_anthropic_cls:
        mock_settings.has_anthropic_key = True
        mock_settings.anthropic_api_key = "sk-ant-api03-TOTALLYREALSECRET"
        mock_settings.jarvis_ai_provider = "anthropic"
        mock_settings.jarvis_ai_model = "claude-haiku-4-5-20251001"
        mock_settings.jarvis_ai_max_tokens = 250
        mock_settings.jarvis_ai_timeout_seconds = 20

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = Exception(sensitive_detail)

        r = api_client.post("/command", json={"command": "tell me something only the AI would answer"})

    assert r.status_code == 200
    body_text = r.text
    assert "sk-ant-api03-TOTALLYREALSECRET" not in body_text
    assert "/home/realuser/.env" not in body_text
    assert "internal-host-7.corp" not in body_text
    assert "Bearer" not in body_text

    body = r.json()
    error = body.get("data", {}).get("error")
    assert error is not None
    assert error["correlation_id"]


# --- release-gate: real timeout enforcement through the full pipeline ---

def _register_hanging_tool_once(name: str, permission_level, timeout_seconds: float):
    """Register a deliberately hanging tool onto the REAL shared registry
    (idempotent — safe to call from multiple tests), matching how
    brain.initialise() itself idempotently populates that same registry.
    """
    import time

    from app.core.brain import brain
    from app.core.models import PermissionLevel, ToolCategory, ToolDefinition
    from app.core.tool_registry import registry

    brain.initialise()
    if registry.get(name) is not None:
        return

    registry.register(
        ToolDefinition(
            name=name,
            description="Release-gate test tool that never returns in time.",
            permission_level=permission_level,
            category=ToolCategory.UTILITY,
            timeout_seconds=timeout_seconds,
        ),
        lambda: time.sleep(30),
    )


def test_auto_execute_timeout_is_recorded_and_reported(api_client):
    from app.core.action_lifecycle import list_recent
    from app.core.models import PermissionLevel
    from app.core.router import ROUTES, Route

    _register_hanging_tool_once("hang_auto_v2", PermissionLevel.SAFE, timeout_seconds=0.2)
    extended_routes = list(ROUTES) + [Route(r"^hang\s+auto\s+v2$", "hang_auto_v2")]

    last_seq = event_bus.latest_seq()
    with patch("app.core.router.ROUTES", extended_routes):
        r = api_client.post("/command", json={"command": "hang auto v2"})

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False

    recent = list_recent(limit=10)
    match = next((rec for rec in recent if rec.tool_name == "hang_auto_v2"), None)
    assert match is not None
    assert match.status == ActionLifecycleStatus.FAILED
    assert match.error_category == "tool_timeout"

    new_events = _events_since(last_seq)
    result_events = [e for e in new_events if e.type == EventType.ACTION_RESULT]
    assert any(e.payload.get("timed_out") is True for e in result_events)


def test_approval_timeout_marks_action_failed_and_blocks_reconfirm(api_client):
    """Full proof of 'protection against approving or executing the same
    timed-out action again': confirm a sensitive action whose tool hangs
    past its timeout, verify it's recorded as failed/timed-out, then
    prove a second confirm attempt on the exact same action_id is
    refused — it can never be executed."""
    from app.core.models import PermissionLevel

    _register_hanging_tool_once("hang_approval_v2", PermissionLevel.APPROVAL_REQUIRED, timeout_seconds=0.2)

    from app.core.router import ROUTES, Route
    extended_routes = list(ROUTES) + [Route(r"^hang\s+approval\s+v2$", "hang_approval_v2")]

    with patch("app.core.router.ROUTES", extended_routes):
        r = api_client.post("/command", json={"command": "hang approval v2"})
    action_id = r.json()["pending_action_id"]

    confirm = api_client.post(f"/actions/{action_id}/confirm")
    assert confirm.json()["success"] is False
    assert confirm.json()["status"] == "failed"

    from app.core.action_lifecycle import get as lifecycle_get
    record = lifecycle_get(action_id)
    assert record.status == ActionLifecycleStatus.FAILED
    assert record.error_category == "tool_timeout"

    # The actual protection: a second confirm on the same action must not execute.
    second_confirm = api_client.post(f"/actions/{action_id}/confirm")
    assert second_confirm.json()["success"] is False
    assert second_confirm.json()["status"] == "failed"
    assert "cannot be confirmed" in second_confirm.json()["message"].lower()
