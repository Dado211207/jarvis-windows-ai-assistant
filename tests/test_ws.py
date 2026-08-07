"""Tests for app/api/ws.py — the typed WebSocket event stream.

Uses FastAPI's TestClient (Starlette under the hood), which runs the real
ASGI app including the real origin check and the real EventBus — no
mocking of the WS transport itself, only of anything unrelated it might
otherwise touch (none, in this module: the WS route only reads
runtime_state/events, it never touches the database).
"""

import json

import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.events import EventType, event_bus


@pytest.fixture(scope="module")
def api_client():
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield client


def test_ws_connects_and_sends_runtime_state_snapshot_first(api_client):
    with api_client.websocket_connect("/ws/events") as ws:
        first = json.loads(ws.receive_text())
    assert first["type"] == EventType.RUNTIME_STATE.value
    assert first["seq"] == 0
    assert "to" in first["payload"]


def test_ws_streams_new_events_after_connecting(api_client):
    with api_client.websocket_connect("/ws/events") as ws:
        ws.receive_text()  # snapshot

        event_bus.publish(EventType.SYSTEM_HEALTH, {"status": "ok"})

        message = json.loads(ws.receive_text())
    assert message["type"] == EventType.SYSTEM_HEALTH.value
    assert message["payload"] == {"status": "ok"}


def test_ws_rejects_disallowed_origin(api_client):
    with pytest.raises(WebSocketDisconnect):
        with api_client.websocket_connect(
            "/ws/events", headers={"origin": "http://evil.example.com"}
        ):
            pass


def test_ws_accepts_allowed_origin_explicitly(api_client):
    from app.api.origin import allowed_origins

    origin = allowed_origins()[0]
    with api_client.websocket_connect("/ws/events", headers={"origin": origin}) as ws:
        first = json.loads(ws.receive_text())
    assert first["type"] == EventType.RUNTIME_STATE.value


def test_ws_since_resumes_after_a_given_sequence_number(api_client):
    published = event_bus.publish(EventType.SYSTEM_HEALTH, {"marker": "resume-test"})

    with api_client.websocket_connect(f"/ws/events?since={published.seq}") as ws:
        first = json.loads(ws.receive_text())
        assert first["type"] == EventType.RUNTIME_STATE.value  # snapshot always first

        event_bus.publish(EventType.SYSTEM_HEALTH, {"marker": "after-resume"})
        second = json.loads(ws.receive_text())

    assert second["payload"] == {"marker": "after-resume"}


def test_ws_event_payload_is_valid_json_with_no_raw_exception_text(api_client):
    """Sanity check on the wire format itself: every message is a clean,
    parseable JSON object with the documented envelope fields, nothing
    Python-repr-shaped (which would suggest a raw exception or object
    leaking onto the socket)."""
    with api_client.websocket_connect("/ws/events") as ws:
        first = json.loads(ws.receive_text())
    assert set(first.keys()) >= {"seq", "type", "timestamp", "payload"}
    assert "Traceback" not in json.dumps(first)
