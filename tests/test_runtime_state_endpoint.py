"""`GET /runtime/state` reports a state *and* how recent that answer is.

Without the sequence the page has two sources of runtime state — this
response and the `/ws/events` stream — and no way to tell which describes
the later moment. It guessed, by applying whichever arrived last, and
tests/test_runtime_ordering.py records the three defects that produced.
"""

import inspect

import pytest


@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app

    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield client


def test_the_snapshot_reports_a_state_and_a_sequence(api_client):
    r = api_client.get("/runtime/state")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["state"], str) and body["state"]
    assert isinstance(body["seq"], int)


def test_the_sequence_is_the_event_stream_s_own(api_client):
    """Not a counter of this endpoint's own making. The number is only
    useful because the client can compare it with the `seq` on a
    `runtime_state` event, which means it has to come from the same
    place."""
    from app.core.events import EventType, event_bus

    before = api_client.get("/runtime/state").json()["seq"]
    event_bus.publish(EventType.RUNTIME_STATE, {"from": "standby", "to": "standby"})
    after = api_client.get("/runtime/state").json()["seq"]

    assert after > before
    assert after == event_bus.latest_seq()


def test_the_snapshot_moves_with_a_real_transition(api_client):
    from app.core.runtime_state import RuntimeState, runtime

    runtime.force_state(RuntimeState.STANDBY)
    first = api_client.get("/runtime/state").json()
    assert first["state"] == "standby"

    runtime.transition(RuntimeState.THINKING, reason="test")
    second = api_client.get("/runtime/state").json()

    assert second["state"] == "thinking"
    assert second["seq"] > first["seq"]
    runtime.force_state(RuntimeState.STANDBY)


def test_the_sequence_is_read_before_the_state():
    """The two reads are not one atomic observation, and only one order of
    them is safe.

    `transition()` assigns the new state under its lock and publishes the
    event after releasing it. Reading the state first and the sequence
    second can therefore return the *old* state under a *new* sequence —
    a snapshot claiming to be as recent as an event it predates, which
    makes the client discard that event as stale and keep showing the
    older state. Reading the sequence first can only under-claim, and an
    event that repeats a state the page already shows changes nothing.

    Checked here rather than left to a comment because swapping two
    adjacent lines is an easy, invisible edit with a consequence nobody
    would connect back to it.
    """
    from app.api import routes

    source = inspect.getsource(routes.runtime_state)
    body = source[source.index('"""', source.index('"""') + 3):]
    assert "latest_seq()" in body and "runtime.state" in body
    assert body.index("latest_seq()") < body.index("runtime.state"), (
        "read the sequence before the state — see this test's docstring"
    )


def test_the_snapshot_needs_no_session_token(api_client):
    """A plain read with no side effects. The state name is already
    broadcast to every connected client over `/ws/events`, so requiring a
    token here would protect nothing and break the page load that needs
    it."""
    r = api_client.get("/runtime/state")
    assert r.status_code == 200
