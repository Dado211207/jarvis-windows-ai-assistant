"""Tests for app/launcher/ipc.py.

The validation functions are pure, so malformed/hostile input is tested
directly with no sockets. The listener/client pair is additionally
exercised over a *real* loopback multiprocessing connection — including a
real failed authentication with the wrong key — because "does the
authentication actually reject a wrong secret" is precisely the property
that would be worthless to assert against a mock.
"""

import threading

import pytest

from app.launcher import ipc


# ---------------------------------------------------------------------------
# Command validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", sorted(ipc.VALID_COMMANDS))
def test_every_known_command_validates(command):
    assert ipc.validate_command({"command": command}) == command


@pytest.mark.parametrize("hostile", [
    None, 42, "show", b"show", [], {},
    {"command": "rm -rf /"},
    {"command": "SHOW"},           # case matters; no fuzzy matching
    {"command": ["show"]},
    {"cmd": "show"},
    {"command": None},
])
def test_malformed_or_unknown_commands_are_rejected(hostile):
    assert ipc.validate_command(hostile) is None


# ---------------------------------------------------------------------------
# Event validation
# ---------------------------------------------------------------------------

def test_ready_event_validates():
    assert ipc.validate_event({"event": "ready"}) == {"event": "ready"}


def test_closed_event_requires_a_known_reason():
    assert ipc.validate_event({"event": "closed", "reason": "user_closed"})["reason"] == "user_closed"
    assert ipc.validate_event({"event": "closed", "reason": "quit_command"})["reason"] == "quit_command"


@pytest.mark.parametrize("hostile", [
    {"event": "closed"},                       # missing reason
    {"event": "closed", "reason": "whatever"},  # unknown reason
    {"event": "closed", "reason": 7},
    {"event": "exploded"},
    "closed",
    None,
])
def test_malformed_events_are_rejected(hostile):
    assert ipc.validate_event(hostile) is None


def test_error_event_coerces_a_non_string_detail():
    assert ipc.validate_event({"event": "error", "detail": 99})["detail"] == ""


# ---------------------------------------------------------------------------
# Address parsing — loopback only
# ---------------------------------------------------------------------------

def test_parse_address_accepts_loopback():
    assert ipc.parse_address("127.0.0.1:54321") == ("127.0.0.1", 54321)


@pytest.mark.parametrize("value", [
    "", "127.0.0.1", "54321", "127.0.0.1:notaport",
    "0.0.0.0:5555",        # never dial a non-loopback address
    "10.0.0.5:5555",
    "evil.example.com:80",
])
def test_parse_address_rejects_anything_else(value):
    assert ipc.parse_address(value) is None


# ---------------------------------------------------------------------------
# Child context from environment
# ---------------------------------------------------------------------------

def _valid_env(**overrides) -> dict:
    env = {
        ipc.IPC_ADDRESS_ENV: "127.0.0.1:5000",
        ipc.IPC_SECRET_ENV: "abc",
        ipc.IPC_URL_ENV: "http://127.0.0.1:5555/ui/",
        ipc.IPC_CLOSE_ACTION_ENV: "quit",
    }
    env.update(overrides)
    return env


def test_child_context_parses_a_complete_environment():
    context = ipc.child_context_from_env(_valid_env())
    assert context["address"] == ("127.0.0.1", 5000)
    assert context["secret"] == b"abc"
    assert context["close_action"] == "quit"


@pytest.mark.parametrize("missing", [ipc.IPC_ADDRESS_ENV, ipc.IPC_SECRET_ENV, ipc.IPC_URL_ENV])
def test_child_context_is_none_when_any_part_is_missing(missing):
    """Fail-safe: `JARVIS.exe --window` run by hand, with no parent, must
    be detectable so it can exit with a clear message rather than a
    traceback."""
    assert ipc.child_context_from_env(_valid_env(**{missing: ""})) is None


def test_child_context_defaults_close_action_when_absent():
    env = _valid_env()
    del env[ipc.IPC_CLOSE_ACTION_ENV]
    assert ipc.child_context_from_env(env)["close_action"] == "tray"


# ---------------------------------------------------------------------------
# Real authenticated round trip
# ---------------------------------------------------------------------------

@pytest.fixture
def listener():
    secret = ipc.generate_secret()
    listener = ipc.ControlListener(secret)
    yield listener, secret
    listener.close()


def test_generated_secrets_are_unique_bytes():
    first, second = ipc.generate_secret(), ipc.generate_secret()
    assert isinstance(first, bytes) and len(first) >= 32
    assert first != second


def test_listener_binds_loopback_only(listener):
    listener_obj, _ = listener
    assert listener_obj.address[0] == "127.0.0.1"


def test_authenticated_round_trip_delivers_commands_and_events(listener):
    listener_obj, secret = listener
    client_box = {}

    def _child():
        client_box["client"] = ipc.ControlClient(listener_obj.address, secret)

    thread = threading.Thread(target=_child, daemon=True)
    thread.start()
    assert listener_obj.accept() is True
    thread.join(timeout=5)
    client = client_box["client"]

    try:
        assert listener_obj.send_command(ipc.COMMAND_SHOW) is True
        assert client.poll_command(timeout=5) == ipc.COMMAND_SHOW

        client.send_event(ipc.EVENT_CLOSED, reason=ipc.REASON_USER_CLOSED)
        event = listener_obj.poll_event(timeout=5)
        assert event == {"event": "closed", "reason": "user_closed"}
    finally:
        client.close()


def test_a_client_with_the_wrong_secret_is_rejected(listener):
    """The whole point of the authkey. Proven against a real connection,
    not a mock — a mocked handshake would prove nothing about whether
    authentication actually happens."""
    listener_obj, _ = listener
    failure = {}

    def _impostor():
        try:
            ipc.ControlClient(listener_obj.address, b"wrong-secret-entirely")
            failure["connected"] = True
        except Exception as exc:
            failure["error"] = type(exc).__name__

    thread = threading.Thread(target=_impostor, daemon=True)
    thread.start()
    accepted = listener_obj.accept()
    thread.join(timeout=5)

    assert failure.get("connected") is not True, "a wrong authkey must never establish a session"
    assert accepted is False


def test_listener_refuses_to_send_an_unknown_command(listener):
    listener_obj, _ = listener
    assert listener_obj.send_command("self-destruct") is False


def test_poll_event_returns_none_when_nothing_is_connected(listener):
    listener_obj, _ = listener
    assert listener_obj.poll_event(timeout=0) is None


def test_malformed_wire_message_is_dropped_not_acted_on(listener):
    """A peer that holds the secret can still send nonsense (a bug, a
    version mismatch); it must be discarded rather than dispatched."""
    listener_obj, secret = listener
    client_box = {}

    def _child():
        client_box["client"] = ipc.ControlClient(listener_obj.address, secret)

    thread = threading.Thread(target=_child, daemon=True)
    thread.start()
    assert listener_obj.accept() is True
    thread.join(timeout=5)
    client = client_box["client"]

    try:
        client._conn.send({"event": "not-a-real-event"})
        assert listener_obj.poll_event(timeout=2) is None
    finally:
        client.close()
