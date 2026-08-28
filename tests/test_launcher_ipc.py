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


# A timed wait can finish a hair before the clock it is measured against
# agrees that its deadline passed. Thread.join() converts its timeout
# once and then waits on an OS primitive whose granularity is the system
# tick — 15.625 ms on Windows by default — while time.monotonic() reads
# the high-resolution counter. A Windows CI runner measured 0.4999999997
# for a 0.5 s join and failed an exact `0.5 <= elapsed`. This tolerance
# is two orders of magnitude below any real defect (an accept() that
# ignored its timeout returns in microseconds) and comfortably above the
# granularity that produced the shortfall. It is not a widened timeout:
# the upper bound, which is what proves accept() cannot block forever,
# is untouched.
CLOCK_SLACK_SECONDS = 0.05


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


@pytest.mark.parametrize("detail", sorted(ipc.VALID_ERROR_DETAILS))
def test_every_named_failure_cause_survives_the_wire(detail):
    """A missing WebView2 runtime and "something else broke" need
    different repairs, so the cause has to reach the parent intact rather
    than being flattened into one generic failure."""
    assert ipc.validate_event({"event": "error", "detail": detail})["detail"] == detail


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


def test_accept_gives_up_instead_of_blocking_forever(listener, caplog):
    """The defect this exists for: accept() had no timeout at all, so a
    window child that died before connecting parked the parent's *main
    thread* permanently — no window, no tray, no error dialog, and no way
    to quit short of Task Manager. Nothing ever connects here, so a
    regression would hang this test rather than fail it; the elapsed-time
    assertion is what turns that into a real failure."""
    import time

    listener_obj, _ = listener

    with caplog.at_level("ERROR"):
        started = time.monotonic()
        accepted = listener_obj.accept(timeout_seconds=0.5)
        elapsed = time.monotonic() - started

    assert accepted is False
    assert elapsed >= 0.5 - CLOCK_SLACK_SECONDS, "accept() returned without waiting its deadline"
    assert elapsed < 5.0, "accept() must return on its own deadline"
    # The diagnostic has to name the deadline that actually elapsed; the
    # first version formatted it with %.0f and reported this wait, which
    # is the one the test just measured at half a second, as "within 0s".
    assert any("within 0.5s" in record.message for record in caplog.records), \
        "the abandonment log must report the real timeout"


def test_a_timed_out_accept_releases_its_waiting_thread(listener):
    """Closing the listener is what actually unblocks the thread parked
    in accept(); without it, one abandoned thread would survive per
    attempt for the life of the process."""
    import threading as _threading
    import time

    listener_obj, _ = listener

    before = {t.name for t in _threading.enumerate()}
    assert listener_obj.accept(timeout_seconds=0.3) is False

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        leaked = {t.name for t in _threading.enumerate()} - before
        if not any(name.startswith("jarvis-ipc-accept") for name in leaked):
            return
        time.sleep(0.05)
    pytest.fail("the accept() worker thread was never released")


def test_wait_for_event_returns_the_awaited_event(listener):
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
        client.send_event(ipc.EVENT_READY)
        assert listener_obj.wait_for_event(ipc.EVENT_READY, timeout_seconds=5) == {"event": "ready"}
    finally:
        client.close()


def test_wait_for_event_returns_an_error_instead_of_waiting_out_the_timeout(listener):
    """A child that reports it cannot build a window has said everything
    it is going to say. Waiting the full ready timeout after that would
    delay the user's error dialog by half a minute for no reason."""
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
        client.send_event(ipc.EVENT_ERROR, detail=ipc.ERROR_WEBVIEW2_MISSING)
        event = listener_obj.wait_for_event(ipc.EVENT_READY, timeout_seconds=30)
        assert event == {"event": "error", "detail": ipc.ERROR_WEBVIEW2_MISSING}
    finally:
        client.close()


def test_wait_for_event_skips_traffic_that_arrives_first(listener):
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
        client.send_event(ipc.EVENT_CLOSED, reason=ipc.REASON_USER_CLOSED)
        client.send_event(ipc.EVENT_READY)
        assert listener_obj.wait_for_event(ipc.EVENT_READY, timeout_seconds=5) == {"event": "ready"}
    finally:
        client.close()


def test_wait_for_event_times_out_when_the_child_goes_quiet(listener):
    """A child that connected and then hung must not hold the parent
    forever — that is the same defect as the untimed accept(), one step
    later in the handshake."""
    import time

    listener_obj, secret = listener
    client_box = {}

    def _child():
        client_box["client"] = ipc.ControlClient(listener_obj.address, secret)

    thread = threading.Thread(target=_child, daemon=True)
    thread.start()
    assert listener_obj.accept() is True
    thread.join(timeout=5)

    try:
        started = time.monotonic()
        assert listener_obj.wait_for_event(ipc.EVENT_READY, timeout_seconds=0.5) is None
        assert 0.5 <= time.monotonic() - started < 5.0
    finally:
        client_box["client"].close()


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


# ---------------------------------------------------------------------------
# The secret survives the environment
# ---------------------------------------------------------------------------

def test_the_secret_is_ascii_so_it_can_cross_an_environment_variable():
    """The defect that made the installed app open a browser.

    The secret reaches the child through its environment, which is text,
    so it is decoded and re-encoded on the way. `secrets.token_bytes()`
    does not survive that: `errors="ignore"` silently discards every byte
    sequence that is not valid UTF-8. The window child then failed its
    HMAC challenge on every launch of every build, the native window
    never appeared, and the launcher fell back to a browser — three
    modules away from the cause.
    """
    for _ in range(200):
        secret = ipc.generate_secret()
        assert secret.decode("ascii").encode("ascii") == secret


def test_the_old_encoding_really_would_have_corrupted_it():
    """Guards the test above against becoming a tautology: it only means
    something if the round trip is genuinely capable of destroying a
    secret, which is why this asserts the failure mode still exists for
    the encoding that caused it."""
    import secrets as _secrets

    corrupted = sum(
        1 for _ in range(200)
        if (lambda b: b.decode("utf-8", errors="ignore").encode("utf-8") != b)(_secrets.token_bytes(32))
    )
    assert corrupted > 190, "random binary must be shown to be unsafe for this round trip"


def test_the_secret_keeps_its_entropy():
    """ASCII-safe must not mean short. token_urlsafe(32) carries the same
    32 bytes of entropy as token_bytes(32)."""
    secrets_seen = {ipc.generate_secret() for _ in range(100)}

    assert len(secrets_seen) == 100
    assert all(len(s) >= 32 for s in secrets_seen)


def test_a_child_authenticates_with_the_secret_it_actually_inherits():
    """End to end over a real socket, through a real environment dict and
    a real HMAC challenge — the exact path the packaged app takes, and
    the one every existing IPC test skipped by handing the client the
    parent's in-memory bytes directly."""
    from app.launcher.window_process import WindowProcess

    window = WindowProcess(url="http://127.0.0.1:5555/ui/")
    window._secret = ipc.generate_secret()
    window._listener = ipc.ControlListener(window._secret)
    try:
        child_env = window.environment(base_env={})
        context = ipc.child_context_from_env(child_env)
        assert context is not None

        connected = {}

        def _child():
            try:
                connected["client"] = ipc.ControlClient(context["address"], context["secret"])
            except Exception as exc:  # noqa: BLE001
                connected["error"] = exc

        thread = threading.Thread(target=_child, daemon=True)
        thread.start()
        accepted = window._listener.accept(timeout_seconds=10)
        thread.join(timeout=10)

        assert "error" not in connected, f"the inherited secret was rejected: {connected.get('error')}"
        assert accepted is True
        connected["client"].close()
    finally:
        window._listener.close()


def test_a_non_ascii_secret_in_the_environment_is_refused_not_raised():
    """`JARVIS.exe --window` started with a hand-set variable must exit
    cleanly, the same as with no context at all."""
    env = _valid_env(**{ipc.IPC_SECRET_ENV: "sécret-with-accents"})
    assert ipc.child_context_from_env(env) is None
