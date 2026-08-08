"""
Tests for Phase 5: Action Approval & Confirmation System

Coverage:
- Approval-required command creates pending action, does not execute immediately
- Pending action preview fields are safe (no secrets)
- Pending action list endpoint
- Confirm executes exactly once
- Cancel prevents execution
- Cancelled action cannot be confirmed
- Already-executed action cannot execute again
- Invalid action ID returns 404
- Safe commands still execute immediately (no regression)
- Permission model not bypassed via direct registry.execute()
- Action logs record confirmed/cancelled/executed states
- UI actions page returns 200 HTML
- UI navigation includes Actions link
- Chat handles requires_approval response shape
- Expired / non-pending actions are handled safely
"""

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(scope="module")
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield prime_session(client)


@pytest.fixture(autouse=True)
def reset_pending_store():
    """Wipe the pending store before and after every test for isolation."""
    from app.core.pending_actions import pending_store
    with pending_store._lock:
        pending_store._actions.clear()
    yield
    with pending_store._lock:
        pending_store._actions.clear()


# ── Approval-required command flow ────────────────────────────────────────────

def test_approval_required_command_does_not_execute(api_client):
    """clear logs must NOT delete anything — it creates a pending action instead."""
    r = api_client.post("/command", json={"command": "clear logs"})
    assert r.status_code == 200
    body = r.json()
    assert body["requires_approval"] is True
    assert body["pending_action_id"] is not None


def test_approval_required_returns_pending_action_id(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    body = r.json()
    action_id = body["pending_action_id"]
    assert isinstance(action_id, str) and len(action_id) == 36  # UUID


def test_approval_required_command_success_true(api_client):
    """The command was received and handled; success=True means pending, not executed."""
    r = api_client.post("/command", json={"command": "clear logs"})
    body = r.json()
    assert body["success"] is True


def test_approval_required_response_has_preview_data(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    body = r.json()
    data = body.get("data") or {}
    assert "description" in data
    assert "risk_level" in data
    assert "tool_name" in data
    assert data["tool_name"] == "clear_logs"


def test_approval_required_action_is_in_pending_store(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]

    r2 = api_client.get(f"/actions/{action_id}")
    assert r2.status_code == 200
    assert r2.json()["status"] == "pending"


# ── Pending action model / preview safety ─────────────────────────────────────

def test_pending_action_preview_has_required_fields(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]

    r2 = api_client.get(f"/actions/{action_id}")
    body = r2.json()
    for field in ("id", "command", "tool_name", "action_name", "description",
                  "risk_level", "parameters", "status", "created_at"):
        assert field in body, f"Missing field: {field}"


def test_pending_action_no_secrets_in_preview(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]
    r2 = api_client.get(f"/actions/{action_id}")
    text = r2.text
    assert "ANTHROPIC_API_KEY" not in text
    assert "sk-" not in text


def test_command_response_no_secrets(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-" not in r.text


def test_pending_action_risk_level_is_set(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]
    r2 = api_client.get(f"/actions/{action_id}")
    assert r2.json()["risk_level"] in ("low", "medium", "high")


# ── Pending action list ───────────────────────────────────────────────────────

def test_pending_actions_list_is_empty_initially(api_client):
    r = api_client.get("/actions/pending")
    assert r.status_code == 200
    assert r.json() == []


def test_pending_actions_list_contains_created_action(api_client):
    api_client.post("/command", json={"command": "clear logs"})
    r = api_client.get("/actions/pending")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_pending_actions_list_returns_correct_tool(api_client):
    api_client.post("/command", json={"command": "clear logs"})
    r = api_client.get("/actions/pending")
    actions = r.json()
    assert actions[0]["tool_name"] == "clear_logs"
    assert actions[0]["status"] == "pending"


# ── Confirm flow ──────────────────────────────────────────────────────────────

def test_confirm_executes_action(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]

    r2 = api_client.post(f"/actions/{action_id}/confirm")
    body = r2.json()
    assert r2.status_code == 200
    assert body["success"] is True
    assert body["status"] == "executed"


def test_confirm_marks_action_as_executed(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]
    api_client.post(f"/actions/{action_id}/confirm")

    r2 = api_client.get(f"/actions/{action_id}")
    assert r2.json()["status"] == "executed"


def test_confirm_removes_from_pending_list(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]
    api_client.post(f"/actions/{action_id}/confirm")

    r2 = api_client.get("/actions/pending")
    ids = [a["id"] for a in r2.json()]
    assert action_id not in ids


def test_confirm_cannot_execute_twice(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]

    api_client.post(f"/actions/{action_id}/confirm")
    r2 = api_client.post(f"/actions/{action_id}/confirm")
    body = r2.json()
    # Second confirm must not re-execute
    assert body["success"] is False
    assert body["status"] in ("executed", "failed")


# ── Cancel flow ───────────────────────────────────────────────────────────────

def test_cancel_action_succeeds(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]

    r2 = api_client.post(f"/actions/{action_id}/cancel")
    body = r2.json()
    assert r2.status_code == 200
    assert body["success"] is True
    assert body["status"] == "cancelled"


def test_cancelled_action_cannot_be_confirmed(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]

    api_client.post(f"/actions/{action_id}/cancel")
    r2 = api_client.post(f"/actions/{action_id}/confirm")
    body = r2.json()
    # Must not execute
    assert body["success"] is False
    assert body["status"] == "cancelled"


def test_cancel_removes_from_pending_list(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]
    api_client.post(f"/actions/{action_id}/cancel")

    r2 = api_client.get("/actions/pending")
    ids = [a["id"] for a in r2.json()]
    assert action_id not in ids


def test_cancel_logs_blocked_status(api_client):
    """Cancellation must write status='blocked' to action_logs."""
    r = api_client.post("/command", json={"command": "clear logs"})
    action_id = r.json()["pending_action_id"]
    api_client.post(f"/actions/{action_id}/cancel")

    logs_r = api_client.get("/logs?limit=5")
    logs = logs_r.json()
    blocked = [l for l in logs if l.get("status") == "blocked" and l.get("tool_name") == "clear_logs"]
    assert len(blocked) >= 1


# ── Invalid / unknown action ID ───────────────────────────────────────────────

def test_get_nonexistent_action_returns_404(api_client):
    r = api_client.get("/actions/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_confirm_nonexistent_action_returns_404(api_client):
    r = api_client.post("/actions/00000000-0000-0000-0000-000000000000/confirm")
    assert r.status_code == 404


def test_cancel_nonexistent_action_returns_404(api_client):
    r = api_client.post("/actions/00000000-0000-0000-0000-000000000000/cancel")
    assert r.status_code == 404


# ── Safe commands are unaffected ──────────────────────────────────────────────

def test_safe_command_executes_immediately(api_client):
    r = api_client.post("/command", json={"command": "status"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body.get("requires_approval", False) is False
    assert body.get("pending_action_id") is None


def test_safe_command_help_executes_immediately(api_client):
    r = api_client.post("/command", json={"command": "help"})
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert r.json().get("requires_approval", False) is False


# ── Permission model not bypassed ─────────────────────────────────────────────

def test_direct_registry_execute_still_blocked():
    """registry.execute() must not run an APPROVAL_REQUIRED tool directly."""
    from app.core.tool_registry import registry
    result = registry.execute("clear_logs")
    # Should return failure — permission check blocks it
    assert result["success"] is False
    assert "approval" in result["message"].lower()


def test_execute_approved_runs_tool():
    """execute_approved() is the only path that runs approval-required tools."""
    from app.core.tool_registry import registry
    result = registry.execute_approved("clear_logs")
    assert result["success"] is True


# ── UI actions page ───────────────────────────────────────────────────────────

def test_ui_actions_returns_200_html(api_client):
    r = api_client.get("/ui/actions")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_ui_actions_has_expected_elements(api_client):
    r = api_client.get("/ui/actions")
    html = r.text
    assert "actions-list" in html
    assert "actions-refresh" in html


def test_ui_nav_includes_actions_link(api_client):
    r = api_client.get("/ui/")
    assert "/ui/actions" in r.text


def test_ui_nav_actions_link_on_all_pages(api_client):
    for path in ("/ui/", "/ui/chat", "/ui/logs", "/ui/memory", "/ui/voice", "/ui/help", "/ui/setup"):
        r = api_client.get(path)
        assert "/ui/actions" in r.text, f"Actions link missing on {path}"


def test_ui_actions_no_api_key(api_client):
    r = api_client.get("/ui/actions")
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-" not in r.text


# ── Chat page requires_approval handling ─────────────────────────────────────

def test_chat_page_still_loads(api_client):
    r = api_client.get("/ui/chat")
    assert r.status_code == 200


def test_js_handles_requires_approval_field():
    """app.js must branch on requires_approval without using innerHTML."""
    import os
    js_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "ui", "static", "app.js"
    )
    # app.js is UTF-8 (it contains non-ASCII characters, e.g. curly quotes)
    # and is served as such (base.html declares <meta charset="UTF-8">).
    # open() without an explicit encoding uses the platform's default
    # locale encoding, which on Windows is a codepage like cp1252, not
    # UTF-8 — that silently produces wrong text, or as here, crashes
    # outright on a byte cp1252 has no mapping for.
    with open(js_path, encoding="utf-8") as f:
        js = f.read()
    assert "requires_approval" in js
    assert "pending_action_id" in js
    assert "innerHTML" not in js


def test_js_calls_confirm_and_cancel_endpoints():
    import os
    js_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "ui", "static", "app.js"
    )
    with open(js_path, encoding="utf-8") as f:
        js = f.read()
    assert "/confirm" in js
    assert "/cancel" in js


# ── Health / phase ────────────────────────────────────────────────────────────

def test_health_reports_current_phase(api_client):
    r = api_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["phase"]  # non-empty; naming convention may evolve (e.g. "v0.2: ...")


def test_version_is_updated(api_client):
    r = api_client.get("/")
    assert r.status_code == 200
    assert r.json()["version"]  # non-empty; exact value tracked by tests/test_safe_actions.py
