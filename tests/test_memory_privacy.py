"""Tests for memory management and the privacy controls around it.

Two things are being pinned here. First, that the Memory page can
actually manage memory — before this it could only search, so saving
required typing a command in a different page and deleting was
impossible from anywhere.

Second, and more important: that the privacy-mode promise holds no
matter which door the write comes through. A page that saves while
privacy mode says nothing should be saved would make the whole feature
worthless, so the endpoint shares the handler rather than reimplementing
the check.
"""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import prime_session


@pytest.fixture
def client():
    from app.api.server import app
    with TestClient(app) as test_client:
        yield prime_session(test_client)


@pytest.fixture(autouse=True)
def _privacy_off():
    from app.core.privacy import privacy_mode
    privacy_mode.set(False)
    yield
    privacy_mode.set(False)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """A real database of this test's own, so counts and deletions are
    measured rather than mocked."""
    from db.database import Database
    from db.migrations import create_tables

    db_path = tmp_path / "memory_test.db"
    create_tables(db_path=db_path)
    database = Database(db_path=db_path)
    monkeypatch.setattr("db.database.get_db", lambda: database)
    yield database
    database.close()


# ---------------------------------------------------------------------------
# Saving from the page
# ---------------------------------------------------------------------------

def test_a_memory_can_be_saved_from_the_page(client, isolated_db):
    response = client.post("/memory", json={"content": "the wifi password is on the router"})

    assert response.json()["success"] is True
    assert [m.content for m in isolated_db.get_all_memories()] == ["the wifi password is on the router"]


def test_a_blank_memory_is_rejected(client):
    assert client.post("/memory", json={"content": "   "}).status_code == 422


def test_saving_a_memory_requires_the_session_token():
    from app.api.server import app

    with TestClient(app) as bare:
        bare.get("/health")
        bare.cookies.clear()
        assert bare.post("/memory", json={"content": "x"}).status_code == 403


def test_privacy_mode_refuses_a_save_from_the_page_too(client, isolated_db):
    """The endpoint shares add_memory's handler precisely so this cannot
    drift: a second code path would eventually forget the check."""
    from app.core.privacy import privacy_mode

    privacy_mode.set(True)
    response = client.post("/memory", json={"content": "should not be stored"})

    assert response.json()["success"] is False
    assert "privacy mode" in response.json()["message"].lower()
    assert isolated_db.get_all_memories() == []


def test_the_refusal_is_reported_rather_than_silently_dropped(client):
    """A page that says "saved" and did not save is worse than an error."""
    from app.core.privacy import privacy_mode

    privacy_mode.set(True)
    body = client.post("/memory", json={"content": "x"}).json()

    assert body["success"] is False
    assert body["message"]


# ---------------------------------------------------------------------------
# Deleting one
# ---------------------------------------------------------------------------

def test_one_memory_can_be_deleted(client, isolated_db):
    memory_id = isolated_db.add_memory("something to forget")

    response = client.post(f"/memory/{memory_id}/delete", json={})

    assert response.json()["success"] is True
    assert isolated_db.get_all_memories() == []


def test_deleting_one_memory_leaves_the_others(client, isolated_db):
    keep = isolated_db.add_memory("keep this")
    drop = isolated_db.add_memory("drop this")

    client.post(f"/memory/{drop}/delete", json={})

    assert [m.id for m in isolated_db.get_all_memories()] == [keep]


def test_deleting_a_memory_that_is_gone_says_so(client, isolated_db):
    """Reporting success for an id that never existed would tell a user
    something was removed when nothing was."""
    body = client.post("/memory/999999/delete", json={}).json()

    assert body["success"] is False
    assert "no longer exists" in body["message"]


def test_deleting_a_memory_requires_the_session_token():
    from app.api.server import app

    with TestClient(app) as bare:
        bare.get("/health")
        bare.cookies.clear()
        assert bare.post("/memory/1/delete", json={}).status_code == 403


# ---------------------------------------------------------------------------
# Deleting everything is a different decision
# ---------------------------------------------------------------------------

def test_clearing_all_memory_requires_approval(client, isolated_db):
    """Irreversible, easy to trigger by accident, and nothing on screen
    says what would be lost — so it goes through the same gate as
    clearing the action log."""
    isolated_db.add_memory("still here")

    body = client.post("/command", json={"command": "forget everything"}).json()

    assert body["requires_approval"] is True
    assert body["pending_action_id"]
    assert isolated_db.get_all_memories(), "nothing may be deleted before confirmation"


def test_confirming_the_clear_deletes_everything(client, isolated_db):
    isolated_db.add_memory("one")
    isolated_db.add_memory("two")

    created = client.post("/command", json={"command": "clear memory"}).json()
    confirmed = client.post(f"/actions/{created['pending_action_id']}/confirm", json={}).json()

    assert confirmed["success"] is True
    assert isolated_db.get_all_memories() == []


def test_cancelling_the_clear_deletes_nothing(client, isolated_db):
    isolated_db.add_memory("survives")

    created = client.post("/command", json={"command": "forget everything"}).json()
    client.post(f"/actions/{created['pending_action_id']}/cancel", json={})

    assert len(isolated_db.get_all_memories()) == 1


@pytest.mark.parametrize("phrasing", [
    "clear memory", "clear memories", "clear all memories", "forget everything", "forget all memories",
])
def test_bulk_clear_phrasings_all_reach_the_gated_tool(phrasing):
    from app.core.router import find_route

    match = find_route(phrasing)
    assert match is not None, f"{phrasing!r} matched no route"
    assert match[0].tool_name == "clear_memory"


def test_the_approval_preview_says_what_is_not_affected(client, isolated_db):
    """"Delete everything" is frightening precisely because its scope is
    unclear; the preview states the boundary."""
    body = client.post("/command", json={"command": "forget everything"}).json()

    description = body["data"]["description"]
    assert "notes" in description
    assert "cannot be undone" in description


def test_clearing_memory_is_never_auto_executed():
    from app.core.models import RiskLevel
    from app.core.policy import PolicyAction, evaluate, risk_for
    from app.core.brain import brain
    from app.core.tool_registry import registry

    brain.initialise()
    definition = registry.get("clear_memory").definition
    risk = risk_for(definition.permission_level, definition.risk)

    assert evaluate(risk, "clear_memory").action == PolicyAction.REQUIRE_APPROVAL
    assert definition.reversible is False


# ---------------------------------------------------------------------------
# What is stored about you
# ---------------------------------------------------------------------------

def test_the_data_summary_counts_what_is_actually_stored(client, isolated_db):
    isolated_db.add_memory("a")
    isolated_db.add_memory("b")
    isolated_db.add_conversation("user", "hello")

    items = {i["key"]: i["count"] for i in client.get("/privacy/data").json()["items"]}

    assert items["memories"] == 2
    assert items["conversations"] == 1


def test_the_data_summary_returns_counts_not_content(client, isolated_db):
    """This page may be open while someone is looking over a shoulder."""
    isolated_db.add_memory("a private thing I told JARVIS")

    assert "a private thing I told JARVIS" not in client.get("/privacy/data").text


def test_the_data_summary_states_the_database_is_not_encrypted(client):
    """Stated rather than omitted — "stays local" is not the same claim
    as "is protected"."""
    assert client.get("/privacy/data").json()["encrypted"] is False


def test_the_data_summary_survives_a_broken_database(client, monkeypatch):
    """A diagnostics-style page must not break when the thing it is
    describing is broken."""
    monkeypatch.setattr("db.database.get_db", lambda: (_ for _ in ()).throw(OSError("locked")))

    body = client.get("/privacy/data").json()

    assert all(item["count"] == 0 for item in body["items"])


def test_row_counts_cannot_be_asked_for_an_arbitrary_table(isolated_db):
    """A count endpoint is not a reason to open a path from a request to
    arbitrary SQL."""
    with pytest.raises(ValueError):
        isolated_db.count_rows("sqlite_master")


# ---------------------------------------------------------------------------
# Privacy toggle on the Settings page
# ---------------------------------------------------------------------------

def test_the_settings_page_has_a_privacy_toggle(client):
    assert 'id="settings-privacy-toggle"' in client.get("/ui/settings").text


def test_the_settings_page_shows_what_is_stored(client):
    assert 'id="settings-stored-data"' in client.get("/ui/settings").text


def _js() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")


def test_the_toggle_goes_through_the_audited_command_path():
    """Not a second write endpoint: /command is already protected,
    already writes an audit record, and already publishes the event the
    topbar indicator listens for."""
    js = _js()
    toggle = js[js.index("async function setPrivacyMode"):js.index("async function refreshStoredData")]

    assert '"/command"' in toggle
    assert "privacy mode on" in toggle
    assert "privacy mode off" in toggle


def test_a_failed_toggle_reverts_rather_than_showing_a_false_state():
    js = _js()
    toggle = js[js.index("async function setPrivacyMode"):js.index("async function refreshStoredData")]

    assert "toggle.checked = !active" in toggle


def test_the_memory_page_offers_saving_and_deleting(client):
    body = client.get("/ui/memory").text
    for element_id in ("memory-add-input", "memory-add-btn", "memory-add-message"):
        assert f'id="{element_id}"' in body


def test_deleting_a_memory_asks_first():
    js = _js()
    assert "Delete this memory?" in js
    assert "cannot be undone" in js


def test_each_delete_button_says_which_memory_it_removes():
    """A column of identical "Delete" buttons is unusable with a screen
    reader."""
    assert "Delete memory: ${preview}" in _js()


def test_the_memory_page_javascript_never_builds_markup():
    js = _js()
    section = js[js.index("// ── Memory"):js.index("// ── Voice")]
    assert "innerHTML" not in section
