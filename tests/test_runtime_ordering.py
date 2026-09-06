"""What JARVIS is doing must not be decided by which answer arrived last.

Three things can set the runtime indicator — a `runtime_state` event off
`/ws/events`, a `GET /runtime/state` snapshot, and the stream dropping —
and they do not arrive in the order they were made. Before
`applyRuntimeObservation` existed, every one of them was applied
unconditionally, and all three orderings below were reproduced in a real
browser against the previous build (`d8fe2b9`):

  1. a snapshot issued at page load answered "standby" after a live
     "listening" event had already been drawn, and reverted it;
  2. a snapshot still in flight when the socket closed answered
     "speaking" a moment later, repainting an active reply beside a
     topbar reading "reconnecting";
  3. two snapshots completing in reverse order left the older on screen.

These tests are behavioural, not structural: they run the real `app.js`
in a real browser against the real server, with `fetch("/runtime/state")`
replaced by a promise the test resolves by hand at a chosen moment. That
is the only way to make an ordering deterministic — a test that waits and
hopes for a race proves nothing when it passes.

The disconnect is real too: `context.set_offline(True)` drops the
WebSocket, so the page's own `close` handler runs. Nothing here simulates
the thing it is testing.
"""

import pytest

from tests.conftest import BROWSER_BASE_URL as BASE_URL

pytestmark = pytest.mark.browser


@pytest.fixture(scope="session")
def ordering_browser(playwright_instance):
    from tests.conftest import chromium_executable_path

    try:
        browser = playwright_instance.chromium.launch(
            executable_path=chromium_executable_path(),
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium is not available ({exc})")
    yield browser
    browser.close()


@pytest.fixture
def ctx(live_server, ordering_browser):
    context = ordering_browser.new_context()
    yield context
    context.close()


# Keeps a reference to the page's own WebSocket without replacing it.
# `BrowserContext.set_offline` was tried first and does not drop a socket
# that is already established — the connection stayed up and the test
# waited ten seconds for a close that never came. This wrapper hands the
# real socket back to the page unchanged; the test can then call the real
# `close()` on it, so the page's real close handler runs.
CAPTURE_SOCKETS = """
(() => {
  const Native = window.WebSocket;
  window.__sockets = [];
  window.WebSocket = function (...args) {
    const socket = new Native(...args);
    window.__sockets.push(socket);
    return socket;
  };
  window.WebSocket.prototype = Native.prototype;
})();
"""


@pytest.fixture
def page(ctx):
    pg = ctx.new_page()
    pg.add_init_script(CAPTURE_SOCKETS)
    pg.errors = []
    pg.on("pageerror", lambda exc: pg.errors.append(str(exc)))
    yield pg


# `refreshRuntimeState()` normally reaches the server. Replacing `fetch`
# for that one path — and only that path — hands the test the moment the
# response lands, which is the entire variable under examination. Every
# other request still goes to the real server.
CONTROLLED_FETCH = """
() => {
  window.__pending = [];
  const realFetch = window.fetch.bind(window);
  window.fetch = (path, opts) => {
    if (String(path).indexOf("/runtime/state") === 0) {
      return new Promise(resolve => {
        window.__pending.push(body => resolve({
          ok: true, status: 200, statusText: "OK",
          json: async () => body,
        }));
      });
    }
    return realFetch(path, opts);
  };
}
"""


def _open(page, path):
    """Load a page, wait for the stream, and return the server's current
    sequence so the test can build observations that are provably newer
    than anything the page has already applied."""
    page.goto(f"{BASE_URL}{path}", wait_until="networkidle")
    page.wait_for_function(
        "document.getElementById('topbar-ws-label').textContent === 'live'", timeout=10000
    )
    base = page.evaluate("async () => (await (await fetch('/runtime/state')).json()).seq")
    assert isinstance(base, int), "GET /runtime/state must report the sequence it was read at"
    page.evaluate(CONTROLLED_FETCH)
    return base


def _open_chat(page):
    return _open(page, "/ui/chat")


def _open_home(page):
    return _open(page, "/ui/")


def _core(page):
    return page.evaluate("() => document.getElementById('runtime-core').dataset.state")


def _badge(page):
    return page.evaluate("() => document.getElementById('topbar-runtime-label').textContent")


def _ws_label(page):
    return page.evaluate("() => document.getElementById('topbar-ws-label').textContent")


def _start_snapshot(page):
    """Issue a snapshot request and leave it in flight.

    Deliberately not `page.evaluate("() => refreshRuntimeState()")`:
    that function is async, Playwright awaits a returned promise, and the
    call would block until the response this test is holding back.
    """
    before = page.evaluate("() => window.__pending.length")
    page.evaluate("() => { refreshRuntimeState(); }")
    page.wait_for_function(f"() => window.__pending.length > {before}", timeout=5000)


def _resolve_snapshot(page, index, state, seq):
    page.evaluate(
        "([i, s, q]) => window.__pending[i]({state: s, seq: q})", [index, state, seq]
    )
    page.wait_for_timeout(120)


def _deliver_event(page, state, seq):
    page.evaluate(
        """([s, q]) => handleStreamEvent({
            seq: q, type: "runtime_state",
            timestamp: new Date().toISOString(), payload: {to: s},
        })""",
        [state, seq],
    )
    page.wait_for_timeout(60)


# ---------------------------------------------------------------------------
# The three reproduced orderings
# ---------------------------------------------------------------------------

def test_a_delayed_older_snapshot_does_not_overwrite_a_newer_event(page):
    """Reproduction 1. The page-load snapshot is issued first and answers
    last; the live event describes the later moment and must survive."""
    base = _open_chat(page)

    _start_snapshot(page)
    _deliver_event(page, "listening", base + 10)
    assert _core(page) == "listening"

    _resolve_snapshot(page, 0, "standby", base + 5)

    assert _core(page) == "listening", "an older snapshot overwrote a newer event"
    assert _badge(page) == "listening"
    assert page.errors == []


def test_a_snapshot_in_flight_when_the_stream_drops_is_discarded(ctx, page):
    """Reproduction 2, with a real disconnect.

    `set_offline` drops the WebSocket, so the page's own close handler
    runs. The snapshot that was already in flight then answers — and an
    answer about a channel that has since gone down is not evidence about
    now, however recent its sequence looks.
    """
    base = _open_chat(page)
    _start_snapshot(page)

    # Offline first so the automatic reconnect cannot succeed and quietly
    # restore the connection mid-assertion; then close the real socket, so
    # the page's own close handler is what runs.
    ctx.set_offline(True)
    page.evaluate("() => window.__sockets[window.__sockets.length - 1].close()")
    page.wait_for_function(
        "() => document.getElementById('runtime-core').dataset.state === 'disconnected'",
        timeout=10000,
    )
    assert _ws_label(page) != "live"

    _resolve_snapshot(page, 0, "speaking", base + 50)

    assert _core(page) == "disconnected", "a stale snapshot repainted a state nobody was in"
    assert _badge(page) == "unknown"
    assert _ws_label(page) != "live"
    ctx.set_offline(False)


def test_two_snapshots_completing_in_reverse_order_leave_the_newer_showing(page):
    """Reproduction 3. Nothing orders two in-flight requests but the
    sequence each one reports."""
    base = _open_chat(page)

    _start_snapshot(page)   # older request
    _start_snapshot(page)   # newer request
    assert page.evaluate("() => window.__pending.length") == 2

    _resolve_snapshot(page, 1, "thinking", base + 10)
    assert _core(page) == "thinking"

    _resolve_snapshot(page, 0, "standby", base + 5)

    assert _core(page) == "thinking", "the older of two snapshots won on arrival order"


# ---------------------------------------------------------------------------
# Reconnect replay — the case where the snapshot is the *newer* observation
# ---------------------------------------------------------------------------

def test_replayed_events_do_not_overwrite_a_newer_reconnect_snapshot(page):
    """On reconnect the page asks for both: `?since=` replays what was
    missed, and a snapshot reports now. The replay describes moments
    strictly before that snapshot, so a replayed event landing afterwards
    must not win — even though it arrived later."""
    base = _open_chat(page)
    page.evaluate("() => runtimeConnectionLost();")
    assert _core(page) == "disconnected"

    _start_snapshot(page)
    _resolve_snapshot(page, 0, "standby", base + 20)
    assert _core(page) == "standby"

    _deliver_event(page, "listening", base + 12)   # a missed, older transition

    assert _core(page) == "standby", "a replayed older event overwrote the reconnect snapshot"


def test_a_replayed_newer_event_does_win_over_the_reconnect_snapshot(page):
    """The other half, so the rule is an ordering and not a preference for
    snapshots: a replayed transition after the snapshot's sequence is the
    later moment and must be applied."""
    base = _open_chat(page)
    page.evaluate("() => runtimeConnectionLost();")

    _start_snapshot(page)
    _resolve_snapshot(page, 0, "standby", base + 20)
    _deliver_event(page, "thinking", base + 25)

    assert _core(page) == "thinking"
    assert _badge(page) == "thinking"


def test_a_quiet_reconnect_still_recovers_the_state(page):
    """Nothing transitioned while the stream was down, so the reconnect
    snapshot carries the same sequence the page had already applied
    before the drop.

    If a disconnect only invalidated in-flight responses and left the
    applied sequence where it was, that snapshot would be discarded as
    "not newer" and the indicator would sit on "unknown" for as long as
    JARVIS stayed quiet. Losing the connection means the page knows
    nothing — including nothing to compare against.
    """
    base = _open_chat(page)

    # `base + 5`, not `base`: the page applied its own load snapshot at
    # `base`, so a setup observation numbered `base` is not newer and is
    # correctly discarded — which left this test asserting against
    # whatever state an earlier test in the session had transitioned the
    # shared state machine to. It passed alone and failed in the suite.
    # The property under test is that the sequence is *unchanged across
    # the drop*, not what its value is.
    quiet_seq = base + 5

    _start_snapshot(page)
    _resolve_snapshot(page, 0, "standby", quiet_seq)
    assert _core(page) == "standby"

    page.evaluate("() => runtimeConnectionLost();")
    assert _core(page) == "disconnected"

    _start_snapshot(page)
    _resolve_snapshot(page, 1, "standby", quiet_seq)   # the same sequence as before the drop

    assert _core(page) == "standby", "a quiet reconnect left the indicator stuck on unknown"
    assert _badge(page) == "standby"


# ---------------------------------------------------------------------------
# One writer, three indicators
# ---------------------------------------------------------------------------

def test_one_event_moves_the_badge_the_core_and_the_home_card_together(page):
    """What `test_runtime_card_reads_the_same_source_as_the_topbar` used to
    check by reading 400 characters of source, checked by running it.

    Home has the card and no core; Chat has the core. Both are written by
    `applyRuntimeObservation` and by nothing else, so neither can be
    showing a state the other is not.
    """
    base = _open_chat(page)
    _deliver_event(page, "executing", base + 30)
    assert _core(page) == "executing"
    assert _badge(page) == "executing"

    home_base = _open_home(page)
    _deliver_event(page, "speaking", home_base + 30)

    assert _badge(page) == "speaking"
    assert page.evaluate(
        "() => document.getElementById('dash-runtime-state').textContent"
    ) == "speaking"


# ---------------------------------------------------------------------------
# Malformed observations
# ---------------------------------------------------------------------------

def test_an_observation_with_no_sequence_is_not_applied(page):
    """An event or snapshot without a sequence cannot be ordered against
    anything. The server puts one on both, so this is a malformed message
    — and acting on it is the "assume something plausible" the whole
    mechanism exists to stop."""
    base = _open_chat(page)
    _deliver_event(page, "thinking", base + 40)
    assert _core(page) == "thinking"

    page.evaluate(
        """() => handleStreamEvent({
            type: "runtime_state", timestamp: "", payload: {to: "listening"},
        })"""
    )
    page.wait_for_timeout(80)

    assert _core(page) == "thinking", "an unorderable observation was applied anyway"
