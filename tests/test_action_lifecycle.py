"""Tests for app/core/action_lifecycle.py, the v0.2 persisted action audit
trail, and its SQLite backing in db/database.py.

Each test gets a real, isolated temp-file SQLite database (not the
in-memory-only mocking used elsewhere in this suite) because the behavior
under test here — the partial unique index enforcing idempotency, and
duration computed from real stored timestamps — can only be proven against
real SQL, not a mock.
"""

from unittest.mock import patch

import pytest

from app.core.action_lifecycle import (
    TerminalStateError,
    get,
    list_recent,
    propose,
    transition,
)
from app.core.models import ActionLifecycleStatus
from db.database import Database
from db.migrations import create_tables


@pytest.fixture
def test_db(tmp_path):
    """A real, isolated SQLite database for this test only."""
    db_path = tmp_path / "test_jarvis.db"
    create_tables(db_path=db_path)
    return Database(db_path=db_path)


@pytest.fixture(autouse=True)
def _patch_get_db(test_db):
    """Route action_lifecycle.py's internal get_db() calls to the isolated
    test database instead of the real singleton, matching the existing
    patch("db.database.get_db") convention used across this test suite."""
    with patch("db.database.get_db", return_value=test_db):
        yield


# --- propose() ---

def test_propose_creates_a_proposed_record():
    record = propose("open_app", {"app_name": "notepad"}, risk="reversible")
    assert record.status == ActionLifecycleStatus.PROPOSED
    assert record.tool_name == "open_app"
    assert record.risk == "reversible"
    assert record.input_summary == {"app_name": "notepad"}


def test_propose_redacts_sensitive_looking_keys():
    record = propose("read_clipboard", {"password": "hunter2", "note": "fine"})
    assert record.input_summary["password"] == "***redacted***"
    assert record.input_summary["note"] == "fine"


def test_propose_persists_and_is_retrievable():
    record = propose("system_status", {})
    fetched = get(record.id)
    assert fetched is not None
    assert fetched.id == record.id
    assert fetched.tool_name == "system_status"


def test_propose_generates_unique_ids():
    a = propose("system_status", {})
    b = propose("system_status", {})
    assert a.id != b.id


def test_propose_with_idempotency_key_returns_existing_record_on_repeat():
    first = propose("open_app", {"app_name": "notepad"}, idempotency_key="dedupe-1")
    second = propose("open_app", {"app_name": "notepad"}, idempotency_key="dedupe-1")
    assert first.id == second.id
    assert len(list_recent(limit=10)) == 1


def test_propose_without_idempotency_key_never_dedupes():
    a = propose("system_status", {})
    b = propose("system_status", {})
    assert a.id != b.id
    assert len(list_recent(limit=10)) == 2


def test_propose_different_idempotency_keys_create_separate_records():
    a = propose("open_app", {"app_name": "notepad"}, idempotency_key="key-a")
    b = propose("open_app", {"app_name": "notepad"}, idempotency_key="key-b")
    assert a.id != b.id


# --- transition() ---

def test_transition_updates_status():
    record = propose("open_app", {"app_name": "notepad"})
    updated = transition(record.id, ActionLifecycleStatus.EXECUTING)
    assert updated.status == ActionLifecycleStatus.EXECUTING


def test_transition_to_terminal_state_computes_duration():
    record = propose("open_app", {"app_name": "notepad"})
    updated = transition(record.id, ActionLifecycleStatus.SUCCEEDED, result_summary="opened")
    assert updated.status == ActionLifecycleStatus.SUCCEEDED
    assert updated.duration_ms is not None
    assert updated.duration_ms >= 0


def test_transition_of_unknown_action_returns_none():
    assert transition("does-not-exist", ActionLifecycleStatus.EXECUTING) is None


def test_transition_refuses_to_leave_terminal_state():
    record = propose("open_app", {"app_name": "notepad"})
    transition(record.id, ActionLifecycleStatus.SUCCEEDED)
    with pytest.raises(TerminalStateError):
        transition(record.id, ActionLifecycleStatus.EXECUTING)


def test_transition_preserves_terminal_record_unchanged_after_rejection():
    record = propose("open_app", {"app_name": "notepad"})
    transition(record.id, ActionLifecycleStatus.SUCCEEDED, result_summary="opened")
    with pytest.raises(TerminalStateError):
        transition(record.id, ActionLifecycleStatus.FAILED, error_category="should not apply")
    unchanged = get(record.id)
    assert unchanged.status == ActionLifecycleStatus.SUCCEEDED
    assert unchanged.error_category is None


def test_transition_can_carry_arbitrary_extra_fields():
    record = propose("read_clipboard", {})
    updated = transition(
        record.id,
        ActionLifecycleStatus.APPROVED,
        approved_by="user",
        approval_source="dashboard",
    )
    assert updated.approved_by == "user"
    assert updated.approval_source == "dashboard"


def test_blocked_is_a_valid_terminal_transition():
    record = propose("steal_password", {}, risk="blocked")
    updated = transition(record.id, ActionLifecycleStatus.BLOCKED, policy_reason="permanently blocked")
    assert updated.status == ActionLifecycleStatus.BLOCKED
    assert updated.duration_ms is not None


# --- list_recent() ---

def test_list_recent_orders_newest_first():
    first = propose("system_status", {})
    second = propose("open_app", {"app_name": "notepad"})
    results = list_recent(limit=10)
    assert results[0].id == second.id
    assert results[1].id == first.id


def test_list_recent_respects_limit():
    for _ in range(5):
        propose("system_status", {})
    assert len(list_recent(limit=2)) == 2


def test_list_recent_empty_when_nothing_proposed():
    assert list_recent(limit=10) == []


# --- status enum completeness ---

def test_action_lifecycle_status_has_exactly_the_required_values():
    required = {
        "proposed", "pending_approval", "approved", "executing",
        "succeeded", "failed", "cancelled", "expired", "blocked",
    }
    actual = {status.value for status in ActionLifecycleStatus}
    assert actual == required
