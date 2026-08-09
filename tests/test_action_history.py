"""Tests for the action history — the persisted audit trail, and the page
that finally shows it.

The trail has been written since v0.2 and had nowhere to be read, which
made it a promise the product could not demonstrate. These tests cover
the endpoint that exposes it and the two properties that make it worth
having: it records what actually happened (including refusals), and it
never contains a secret.
"""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import prime_session


@pytest.fixture
def client():
    from app.api.server import app
    with TestClient(app) as test_client:
        yield prime_session(test_client)


def _history(client, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"/actions/history{'?' + query if query else ''}").json()


# ---------------------------------------------------------------------------
# What gets recorded
# ---------------------------------------------------------------------------

def test_an_executed_command_appears_in_the_history(client):
    client.post("/command", json={"command": "status"})

    entries = _history(client)["entries"]

    assert any(e["tool_name"] == "status" and e["status"] == "succeeded" for e in entries)


def test_an_approval_required_command_is_recorded_while_still_waiting(client):
    """A proposed action that nobody has answered yet is part of the
    record — "asked and not yet approved" is a real outcome."""
    client.post("/command", json={"command": "read clipboard"})

    entries = _history(client)["entries"]
    clipboard = [e for e in entries if e["tool_name"] == "read_clipboard"]

    assert clipboard and clipboard[0]["status"] == "pending_approval"
    assert clipboard[0]["risk"] == "sensitive"


def test_a_cancelled_action_is_recorded_as_cancelled(client):
    created = client.post("/command", json={"command": "read clipboard"}).json()
    client.post(f"/actions/{created['pending_action_id']}/cancel", json={})

    entries = _history(client)["entries"]
    entry = next(e for e in entries if e["id"] == created["pending_action_id"])

    assert entry["status"] == "cancelled"


def test_the_history_records_the_policy_decision_not_just_the_outcome(client):
    client.post("/command", json={"command": "status"})

    entry = next(e for e in _history(client)["entries"] if e["tool_name"] == "status")

    assert entry["policy_action"] == "auto_execute"
    assert entry["policy_reason"]


def test_entries_are_newest_first(client):
    client.post("/command", json={"command": "status"})
    client.post("/command", json={"command": "disk space"})

    tools = [e["tool_name"] for e in _history(client)["entries"]]

    assert tools.index("disk_space") < tools.index("status")


# ---------------------------------------------------------------------------
# Honesty about how much is being shown
# ---------------------------------------------------------------------------

def test_the_total_counts_everything_held_not_just_what_was_returned(client):
    """Otherwise a capped list silently implies it is the whole record."""
    for _ in range(4):
        client.post("/command", json={"command": "status"})

    body = _history(client, limit=2)

    assert len(body["entries"]) == 2
    assert body["total"] >= 4


def test_the_limit_is_bounded(client):
    """A page cannot ask for the entire table and stall the server."""
    assert len(_history(client, limit=100000)["entries"]) <= 200


@pytest.mark.parametrize("bad_limit", ["0", "-5"])
def test_a_nonsense_limit_still_returns_something(client, bad_limit):
    client.post("/command", json={"command": "status"})
    assert _history(client, limit=bad_limit)["entries"]


def test_filtering_by_outcome_returns_only_that_outcome(client):
    client.post("/command", json={"command": "status"})
    client.post("/command", json={"command": "read clipboard"})

    statuses = {e["status"] for e in _history(client, status="succeeded")["entries"]}

    assert statuses <= {"succeeded"}


def test_an_unknown_filter_value_returns_nothing_rather_than_everything(client):
    """Silently ignoring a filter would show a user the opposite of what
    they asked for."""
    client.post("/command", json={"command": "status"})

    assert _history(client, status="not-a-status")["entries"] == []


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def test_clipboard_content_never_reaches_the_history(client):
    """read_clipboard is the one tool whose output is private by
    definition, and this table is the most durable thing in the app."""
    from unittest.mock import patch

    from app.core.tool_registry import registry

    secret = "hunter2-copied-password"
    created = client.post("/command", json={"command": "read clipboard"}).json()

    # The handler is swapped rather than the clipboard read, so a real
    # value flows through the real confirm/execute path while no actual
    # clipboard is touched — CLAUDE.md forbids that in tests.
    tool = registry.get("read_clipboard")
    original = tool.handler
    tool.handler = lambda: {
        "success": True,
        "message": f"Clipboard contains {len(secret)} character(s).",
        "data": {"content": secret},
    }
    try:
        confirmed = client.post(f"/actions/{created['pending_action_id']}/confirm", json={})
    finally:
        tool.handler = original

    assert secret in confirmed.text, "the secret really did flow through execution"
    assert secret not in client.get("/actions/history").text


def test_a_secret_shaped_tool_input_is_stored_already_masked(client):
    """Inputs are redacted at write time, so nothing here has to trust a
    later filter to keep them out."""
    from app.core.action_lifecycle import propose

    record = propose("some_tool", {"api_key": "sk-should-not-be-stored", "harmless": "ok"})

    raw = client.get("/actions/history").text
    assert "sk-should-not-be-stored" not in raw
    assert record.input_summary["harmless"] == "ok"


def test_the_history_is_readable_without_a_session_token():
    """Read-only, loopback-only, and containing no credential — the same
    reasoning as /logs. Someone checking what happened should not need a
    token to look."""
    from app.api.server import app

    with TestClient(app) as bare:
        bare.get("/health")
        bare.cookies.clear()
        assert bare.get("/actions/history").status_code == 200


def test_history_is_not_shadowed_by_the_action_id_route(client):
    """/actions/{action_id} would swallow "history" if declared first —
    the ordering is load-bearing, so it gets a test."""
    response = client.get("/actions/history")

    assert response.status_code == 200
    assert "entries" in response.json()


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def _js() -> str:
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    return js[js.index("// ── Action history"):js.index("// ── Live event stream")]


@pytest.mark.parametrize("element_id", ["history-tbody", "history-filter", "history-refresh", "history-count"])
def test_the_actions_page_shows_the_history(client, element_id):
    assert f'id="{element_id}"' in client.get("/ui/actions").text


def test_the_page_says_the_record_stays_on_this_computer(client):
    body = client.get("/ui/actions").text
    assert "never sent anywhere" in body


def test_the_page_says_clearing_chat_does_not_clear_this(client):
    """The two are deliberately separate — see
    app/core/conversation.py::reset."""
    assert "Clearing your chat does not clear this history" in client.get("/ui/actions").text


def test_the_history_distinguishes_empty_from_failed_to_load():
    js = _js()
    assert "No actions have been recorded yet." in js
    assert "Could not load the action history." in js


def test_a_filtered_empty_result_says_it_was_filtered():
    """"Nothing here" and "nothing matches this filter" are different
    facts and the user chose one of them."""
    assert "No recorded actions match this filter." in _js()


def test_the_history_javascript_never_builds_markup():
    assert "innerHTML" not in _js()


def test_the_history_refreshes_when_an_action_changes():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'if ($("history-tbody")) loadActionHistory();' in js
