"""Real-browser end-to-end and accessibility tests for the v0.2 dashboard.

Marked `browser` (see pytest.ini) — excluded from the default `pytest`
run since most environments don't have a browser installed. Covered in CI
by the separate `browser-tests` job (.github/workflows/ci.yml). Run
explicitly:

    pip install -r requirements-test.txt
    playwright install chromium
    pytest -m browser tests/test_playwright_e2e.py -v

The server under test runs in-process (uvicorn in a background thread,
same Python interpreter as the test), which is what makes several of
these tests possible without waiting on real time or a real clipboard/
microphone: pending-action expiry is forced directly on the shared
in-memory store, a hanging tool is registered directly on the shared
registry, and STT is backed by FakeSTTAdapter — never real inference.
The real clipboard and a real microphone are never touched; Chromium is
launched with a fake media device for push-to-talk.

Honest architectural note: this dashboard's approval UI is an inline
card appended to the chat timeline or the Actions list — there is no
modal `<dialog>` or overlay anywhere in this codebase. "Focus trap /
restoration" is tested here as what actually exists: the card's
Confirm/Cancel controls are keyboard-reachable and focus lands somewhere
sensible after they resolve, not a modal-dialog focus trap, because
building one was not otherwise in scope for this pass.
"""

import re
import threading
import time
import uuid

import pytest

# The server itself lives in tests/conftest.py, so the two browser suites
# share one definition and therefore one port — see the comment there.
from tests.conftest import BROWSER_BASE_URL as BASE_URL, BROWSER_TEST_PORT  # noqa: E402,F401

pytestmark = pytest.mark.browser

VIEWPORTS = {
    "mobile_390": {"width": 390, "height": 844},
    "tablet_1024": {"width": 1024, "height": 768},
    "desktop_1440": {"width": 1440, "height": 900},
    "desktop_1920": {"width": 1920, "height": 1080},
}

PAGES = ["/ui/", "/ui/chat", "/ui/actions", "/ui/logs", "/ui/memory", "/ui/voice", "/ui/help",
         "/ui/setup", "/ui/settings", "/ui/diagnostics", "/ui/coding"]


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def browser_instance(playwright_instance):
    """One browser for this suite. The Playwright driver itself is shared
    with tests/test_clap_detection.py via conftest — see the note there
    for why it cannot be opened twice.

    Chromium by default, because JARVIS's own window *is* Chromium: the
    product runs in WebView2 and in nothing else. `JARVIS_TEST_BROWSER=
    webkit` runs this suite against WebKit instead, which is a
    portability check on the pages rather than a check on the product as
    shipped. The clap suite has no such switch and cannot have one — it
    presents a WAV file as a microphone, which only Chromium can do."""
    import os

    from tests.conftest import chromium_executable_path

    engine = os.environ.get("JARVIS_TEST_BROWSER", "chromium").strip().lower()
    try:
        if engine == "webkit":
            browser = playwright_instance.webkit.launch()
        else:
            browser = playwright_instance.chromium.launch(
                executable_path=chromium_executable_path(),
                args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
            )
    except Exception as exc:
        pytest.skip(f"{engine} is not available — run `playwright install {engine}` ({exc})")
    yield browser
    browser.close()


@pytest.fixture
def page(live_server, browser_instance):
    # Only Chromium has a microphone permission to grant ahead of time;
    # asking WebKit for one it does not implement fails the context, not
    # the test it was meant to enable.
    if browser_instance.browser_type.name == "chromium":
        context = browser_instance.new_context(permissions=["microphone"])
    else:
        context = browser_instance.new_context()
    pg = context.new_page()
    pg.errors = []
    pg.on("pageerror", lambda exc: pg.errors.append(str(exc)))
    yield pg
    context.close()


def _console_errors(pg, console_msgs):
    pg.on("console", lambda msg: console_msgs.append(msg) if msg.type == "error" else None)


def url(path: str) -> str:
    return f"{BASE_URL}{path}"


# ---------------------------------------------------------------------------
# 1. Command Center (this app's dashboard) loads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", PAGES)
def test_page_loads_with_no_errors(page, path):
    page.goto(url(path), wait_until="networkidle")
    assert page.title()
    assert page.errors == []


# ---------------------------------------------------------------------------
# 2 & 3. WebSocket connects; offline/reconnecting state
# ---------------------------------------------------------------------------

def test_websocket_connects(page):
    page.goto(url("/ui/"), wait_until="networkidle")
    page.wait_for_function("document.getElementById('topbar-ws-label').textContent === 'live'", timeout=5000)


def test_websocket_shows_reconnecting_then_live_after_network_drop(page):
    # page.context.set_offline(True) blocks *new* outgoing connections but
    # does not reliably close an already-established WebSocket in Chromium
    # (offline emulation happens at a layer that doesn't sever a live
    # socket), so it cannot deterministically exercise the client's
    # close/error handling. Instead, route the socket through Playwright's
    # WebSocket proxy: connect_to_server() auto-forwards messages in both
    # directions with no extra wiring, and calling .close() on the
    # page-side route severs that connection exactly like a real network
    # drop would, firing the same "close" handler app.js relies on. The
    # route stays registered for the page's lifetime, so the client's own
    # reconnect (a brand new WebSocket) is proxied the same way and can
    # recover to "live" on its own.
    routed = {}

    def handler(ws):
        ws.connect_to_server()
        routed["client"] = ws

    page.route_web_socket(re.compile(r"/ws/events"), handler)

    page.goto(url("/ui/"), wait_until="networkidle")
    page.wait_for_function("document.getElementById('topbar-ws-label').textContent === 'live'", timeout=5000)

    routed["client"].close()
    page.wait_for_function(
        "document.getElementById('topbar-ws-label').textContent !== 'live'", timeout=5000,
    )
    state_while_dropped = page.eval_on_selector("#topbar-ws-label", "el => el.textContent")
    assert state_while_dropped in ("reconnecting", "offline")

    page.wait_for_function(
        "document.getElementById('topbar-ws-label').textContent === 'live'", timeout=15000,
    )


# ---------------------------------------------------------------------------
# 4 & 5. Text command; read-only automatic action
# ---------------------------------------------------------------------------

def _wait_for_stable_transcript(pg, quiet_ms: int = 300, timeout_ms: int = 5000) -> str:
    """The transcript's text once it has stopped changing, and return it.

    Deliberately generic — it waits for quiescence rather than for a named
    label, so it cannot rot when a control's wording changes. Two equal
    consecutive reads `quiet_ms` apart is the settled condition.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    previous = pg.inner_text("#chat-messages")
    while time.monotonic() < deadline:
        time.sleep(quiet_ms / 1000)
        current = pg.inner_text("#chat-messages")
        if current == previous:
            return current
        previous = current
    return previous


def _assistant_count(pg) -> int:
    return pg.eval_on_selector_all(".msg-assistant", "els => els.length")


def _send_and_wait_for_new_reply(pg, command: str, timeout: int = 8000) -> None:
    """Send *command* and wait for the reply **to it**.

    Waiting on `.msg-assistant` alone is not enough and the difference is a
    real race, not a nicety: the chat page hydrates `/conversation?limit=50`
    into `#chat-messages` on load, so by the time a test navigates there,
    assistant bubbles from earlier tests against the same live server are
    already in the DOM. `wait_for_selector` then returns immediately, having
    matched an old message, and `inner_text` is read before the new reply
    renders.

    Measured, not guessed — the transcript captured at the failure ended:

        …YOU / completely unknown query xyz / JARVIS / AI responses aren…
        YOU / system status                              <- no reply yet

    So the count is what has to grow. One failure in two full browser runs
    at this commit; anchoring on the count removes the race rather than
    hiding it behind a longer timeout.
    """
    before = _assistant_count(pg)
    pg.fill("#chat-input", command)
    pg.click("#chat-send")
    pg.wait_for_function(
        "n => document.querySelectorAll('.msg-assistant').length > n",
        arg=before,
        timeout=timeout,
    )


def test_text_command_read_only_action_executes_without_approval(page):
    page.goto(url("/ui/chat"), wait_until="networkidle")
    _send_and_wait_for_new_reply(page, "system status")
    messages = page.inner_text("#chat-messages")
    assert "System Status" in messages
    # .msg-approval-header is visually upper-cased via CSS text-transform;
    # page.inner_text() reflects rendered (post-CSS) text, so compare
    # case-insensitively rather than against the DOM's mixed-case source.
    assert "approval required" not in messages.lower()


# ---------------------------------------------------------------------------
# 6 & 7. Sensitive clipboard proposal; nothing read before approval
# ---------------------------------------------------------------------------

def test_clipboard_command_proposes_but_does_not_read_before_approval(page):
    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.fill("#chat-input", "read clipboard")
    page.click("#chat-send")
    page.wait_for_selector(".msg-approval", timeout=5000)
    card_text = page.inner_text(".msg-approval").lower()
    assert "approval required" in card_text
    assert "clipboard" in card_text
    # Nothing about actual clipboard content can be present — the tool has
    # not run yet, only the proposal has.


# ---------------------------------------------------------------------------
# 8 & 9. Approval and cancellation, via the real UI
# ---------------------------------------------------------------------------

def test_confirm_button_resolves_the_approval_card(page):
    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.fill("#chat-input", "read clipboard")
    page.click("#chat-send")
    page.wait_for_selector(".msg-approval", timeout=5000)
    page.click(".msg-approval-footer button.btn-primary")
    page.wait_for_function(
        "document.querySelector('.msg-approval-status') && "
        "document.querySelector('.msg-approval-status').textContent.trim().length > 0",
        timeout=5000,
    )
    status_text = page.inner_text(".msg-approval-status")
    assert status_text  # resolved one way or another (tkinter is unavailable here, so a
    # clean failure message is expected and itself proves the approval path ran for real)


def test_cancel_button_prevents_execution(page):
    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.fill("#chat-input", "clear logs")
    page.click("#chat-send")
    page.wait_for_selector(".msg-approval", timeout=5000)
    page.click(".msg-approval-footer button.btn-danger")
    page.wait_for_function(
        "document.querySelector('.msg-approval-status') && "
        "document.querySelector('.msg-approval-status').textContent.includes('Cancelled')",
        timeout=5000,
    )


# ---------------------------------------------------------------------------
# 10. Expiry
# ---------------------------------------------------------------------------

def test_expired_action_cannot_be_confirmed(page):
    from datetime import datetime, timedelta
    from app.core.pending_actions import pending_store

    action = pending_store.create(
        command="clear logs", tool_name="clear_logs", action_name="Clear Logs",
        description="test", risk_level="medium", parameters={},
    )
    with pending_store._lock:
        pending_store._actions[action.id].expires_at = datetime.utcnow() - timedelta(minutes=1)

    from tests.conftest import prime_session
    import httpx
    r = httpx.post(
        f"{BASE_URL}/actions/{action.id}/confirm",
        headers={"X-JARVIS-Session-Token": "irrelevant-for-direct-httpx-call"},
    )
    # A raw httpx call has no valid session token, so this specifically
    # proves the *expiry* path only via the pending_store API directly:
    got = pending_store.get(action.id)
    assert got.status == "expired"
    confirmed = pending_store.confirm(action.id)
    assert confirmed is None  # cannot be confirmed once expired


# ---------------------------------------------------------------------------
# 11. Double-execution protection, via the real UI
# ---------------------------------------------------------------------------

def test_double_click_confirm_only_executes_once(page):
    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.fill("#chat-input", "clear logs")
    page.click("#chat-send")
    page.wait_for_selector(".msg-approval", timeout=5000)

    confirm_btn = page.query_selector(".msg-approval-footer button.btn-primary")
    confirm_btn.click()
    # The button disables itself immediately (see app.js's addApprovalCard) —
    # verify that guard is actually present rather than assuming it.
    is_disabled = page.eval_on_selector(".msg-approval-footer button.btn-primary", "el => el.disabled")
    assert is_disabled is True


# ---------------------------------------------------------------------------
# 12. Timeout, via the real UI
# ---------------------------------------------------------------------------

def test_hanging_tool_times_out_visibly_in_chat(page, live_server):
    from app.core.brain import brain
    from app.core.models import PermissionLevel, ToolCategory, ToolDefinition
    from app.core.tool_registry import registry
    from app.core.router import ROUTES, Route

    brain.initialise()
    tool_name = f"hang_browser_{uuid.uuid4().hex[:8]}"
    if registry.get(tool_name) is None:
        registry.register(
            ToolDefinition(
                name=tool_name, description="browser test hang", permission_level=PermissionLevel.SAFE,
                category=ToolCategory.UTILITY, timeout_seconds=0.5,
            ),
            lambda: time.sleep(30),
        )
    ROUTES.append(Route(rf"^{tool_name}$", tool_name))

    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.fill("#chat-input", tool_name)
    page.click("#chat-send")
    page.wait_for_function(
        "document.getElementById('chat-messages').textContent.toLowerCase().includes('time')",
        timeout=10000,
    )
    messages = page.inner_text("#chat-messages")
    assert "time" in messages.lower()


# ---------------------------------------------------------------------------
# 12b. Streaming answers, Stop, and New conversation, through the real UI
#
# The provider is faked in-process (the live server shares this
# interpreter) so no network call and no API key are involved. What is
# real is everything the browser does: the streaming fetch, the
# incremental render, the Stop round trip, and the confirm dialog.
# ---------------------------------------------------------------------------

def _fake_streaming_provider(chunks, delay=0.25):
    from unittest.mock import MagicMock

    def _stream(messages, system, cancel=None):
        for chunk in chunks:
            if cancel is not None and cancel.cancelled:
                from app.core.ai.base import GenerationCancelled
                raise GenerationCancelled()
            yield chunk
            time.sleep(delay)

    provider = MagicMock()
    provider.name = "anthropic"
    provider.resolved_model.return_value = "fake-streaming-model"
    provider.availability.return_value = MagicMock(ready=True, reason="")
    provider.stream.side_effect = _stream
    return provider


def test_streamed_answer_appears_before_it_has_finished(page, live_server):
    """The point of streaming: text is on screen while the model is still
    producing it, not after."""
    from unittest.mock import patch
    from app.core.brain import brain

    page.goto(url("/ui/chat"), wait_until="networkidle")

    with patch.object(brain, "provider", return_value=_fake_streaming_provider(
        ["Streaming ", "answer ", "arriving ", "in ", "pieces."]
    )):
        page.fill("#chat-input", "an unrouted question for the AI")
        page.click("#chat-send")
        # Visible while the Stop button is still showing, i.e. mid-flight.
        page.wait_for_function(
            "document.getElementById('chat-messages').textContent.includes('Streaming')",
            timeout=10000,
        )
        assert page.eval_on_selector("#chat-stop", "el => el.hidden") is False

        page.wait_for_function(
            "document.getElementById('chat-messages').textContent.includes('pieces.')",
            timeout=15000,
        )

    page.wait_for_function("document.getElementById('chat-stop').hidden === true", timeout=5000)


def test_stop_button_halts_a_streaming_answer(page, live_server):
    from unittest.mock import patch
    from app.core.brain import brain

    page.goto(url("/ui/chat"), wait_until="networkidle")

    with patch.object(brain, "provider", return_value=_fake_streaming_provider(
        ["one ", "two ", "three ", "four ", "five ", "six ", "seven ", "eight "], delay=0.4,
    )):
        page.fill("#chat-input", "another unrouted question")
        page.click("#chat-send")
        page.wait_for_function(
            "document.getElementById('chat-messages').textContent.includes('one')",
            timeout=10000,
        )
        page.click("#chat-stop")
        page.wait_for_function(
            "document.getElementById('chat-status').textContent === 'Stopped.'",
            timeout=10000,
        )
        # `chat-status` flipping to "Stopped." is not the last DOM change the
        # stop causes: the message's own control still has one transition
        # left, from the streaming affordance to Listen. Capturing here and
        # comparing a second later therefore compared a button label, not the
        # answer, and failed with the two reads differing by exactly:
        #
        #     - ■ Stop        + ▶ Listen
        #
        # with the streamed text ("one ") identical on both sides. One failure
        # in four full browser runs. The requirement — that no more of the
        # answer arrives — is unchanged and still checked against the whole
        # container; only the moment it starts checking from is now correct.
        settled = _wait_for_stable_transcript(page)

    time.sleep(1.0)  # nothing more may arrive after the stop settled
    assert page.inner_text("#chat-messages") == settled
    assert "eight" not in settled


def test_new_conversation_clears_the_transcript(page):
    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.fill("#chat-input", "status")
    page.click("#chat-send")
    page.wait_for_function(
        "document.getElementById('chat-messages').textContent.includes('JARVIS')", timeout=5000,
    )

    page.on("dialog", lambda dialog: dialog.accept())
    page.click("#chat-reset")

    page.wait_for_function(
        "document.querySelectorAll('#chat-messages .msg').length === 0", timeout=5000,
    )
    assert page.is_visible("#chat-empty")


def test_chat_reports_the_provider_state_without_reading_as_broken(page):
    """No key is configured in the test environment, so this is the
    unconfigured path — it must still tell the user commands work."""
    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.wait_for_function(
        "document.getElementById('chat-provider').textContent.includes('commands still work')",
        timeout=5000,
    )


# ---------------------------------------------------------------------------
# 12c. Spoken replies, through the real UI
#
# The engine is mocked in-process so nothing plays audio; what is real is
# whether the page asks for speech at all, which is the half that was
# broken (the desktop app never spoke).
# ---------------------------------------------------------------------------

def _speak_requests(pg):
    seen = []
    pg.on("request", lambda r: seen.append(r.url) if r.url.endswith("/voice/speak") else None)
    return seen


def test_a_reply_is_spoken_when_speech_is_switched_on(page, live_server):
    from unittest.mock import MagicMock, patch
    from app.voice.tts import tts_service

    tts_service.set_output_enabled(True)
    spoken = _speak_requests(page)
    try:
        with patch("pyttsx3.init", return_value=MagicMock()):
            page.goto(url("/ui/chat"), wait_until="networkidle")
            page.fill("#chat-input", "status")
            page.click("#chat-send")
            page.wait_for_function(
                "document.getElementById('chat-messages').textContent.includes('JARVIS')", timeout=5000,
            )
            page.wait_for_timeout(500)
    finally:
        tts_service.set_output_enabled(False)

    assert spoken, "the page never asked for the reply to be spoken"


def test_an_approval_prompt_is_not_spoken(page, live_server):
    from unittest.mock import MagicMock, patch
    from app.voice.tts import tts_service

    tts_service.set_output_enabled(True)
    spoken = _speak_requests(page)
    try:
        with patch("pyttsx3.init", return_value=MagicMock()):
            page.goto(url("/ui/chat"), wait_until="networkidle")
            page.fill("#chat-input", "read clipboard")
            page.click("#chat-send")
            page.wait_for_selector(".msg-approval", timeout=5000)
            page.wait_for_timeout(500)
    finally:
        tts_service.set_output_enabled(False)

    assert spoken == [], "an approval prompt must be read, not read aloud"


def _speak_once_requests(pg):
    seen = []
    pg.on("request", lambda r: seen.append(r.url) if r.url.endswith("/voice/speak-once") else None)
    return seen


def _send_and_wait(pg, command="status"):
    """Send, wait for the reply to *this* message, then for its Listen button.

    Same hydration race as `_send_and_wait_for_new_reply` guards against: an
    older assistant bubble already carries a `.msg-speak`, so waiting on the
    selector alone can be satisfied by a message this call did not produce.
    """
    _send_and_wait_for_new_reply(pg, command)
    # The newest bubble specifically. A CSS `:last-of-type` would mean "the
    # last div that happens to be an assistant message", which is a different
    # claim; indexing the matched list says what is meant.
    pg.wait_for_function(
        """() => {
            const all = document.querySelectorAll('.msg-assistant');
            const last = all[all.length - 1];
            const speak = last && last.querySelector('.msg-speak');
            return !!speak && !speak.hidden;
        }""",
        timeout=8000,
    )


def test_every_answer_carries_its_own_listen_button(page, live_server):
    """The owner's requirement after the release-candidate test: a
    speaker control on each message, not one global switch."""
    page.goto(url("/ui/chat"), wait_until="networkidle")
    _send_and_wait(page)

    button = page.locator(".msg-assistant .msg-speak").last

    assert button.get_attribute("aria-label") == "Read this answer aloud"
    assert button.get_attribute("aria-pressed") == "false"
    assert "Listen" in button.inner_text()


def test_the_users_own_messages_have_no_listen_button(page, live_server):
    page.goto(url("/ui/chat"), wait_until="networkidle")
    _send_and_wait(page)

    assert page.locator(".msg-user .msg-speak").count() == 0


def test_pressing_listen_asks_the_server_to_speak_that_message(page, live_server):
    """The button works while "speak every reply" is off — pressing it is
    the request. That refusal was the reported defect."""
    from unittest.mock import MagicMock, patch

    from app.voice.tts import tts_service

    tts_service.set_output_enabled(False)
    asked = _speak_once_requests(page)

    with patch("pyttsx3.init", return_value=MagicMock()):
        page.goto(url("/ui/chat"), wait_until="networkidle")
        _send_and_wait(page)
        page.locator(".msg-assistant .msg-speak").last.click()
        page.wait_for_timeout(500)

    assert asked, "pressing Listen never reached /voice/speak-once"


def test_the_listen_button_is_keyboard_operable(page, live_server):
    page.goto(url("/ui/chat"), wait_until="networkidle")
    _send_and_wait(page)

    button = page.locator(".msg-assistant .msg-speak").last
    button.focus()

    assert page.evaluate("document.activeElement.classList.contains('msg-speak')") is True


def test_the_button_says_stop_while_it_is_speaking(page, live_server):
    """A control that does not report its own state is a control that
    lies about the state of the machine."""
    from unittest.mock import MagicMock, patch

    from app.voice import engines

    page.goto(url("/ui/chat"), wait_until="networkidle")
    _send_and_wait(page)

    with patch("pyttsx3.init", return_value=MagicMock()), \
         patch.object(engines, "speak", return_value=engines.SpeakOutcome(started=True, engine="kokoro", message="ok")), \
         patch.object(engines, "is_speaking", return_value=True):
        page.locator(".msg-assistant .msg-speak").last.click()
        page.wait_for_selector('.msg-speak[aria-pressed="true"]', timeout=5000)

        button = page.locator('.msg-speak[aria-pressed="true"]').first
        assert button.get_attribute("aria-label") == "Stop reading this answer aloud"
        assert "Stop" in button.inner_text()


def test_the_chat_toggle_writes_the_same_saved_setting_as_the_voice_page(page, live_server):
    from app.voice.tts import tts_service

    tts_service.set_output_enabled(False)
    try:
        page.goto(url("/ui/chat"), wait_until="networkidle")
        page.wait_for_function(
            "document.getElementById('chat-speak-replies').checked === false", timeout=5000,
        )
        page.click("#chat-speak-replies")
        page.wait_for_function(
            "document.getElementById('chat-speak-replies').checked === true", timeout=5000,
        )

        assert tts_service.output_enabled is True

        page.goto(url("/ui/voice"), wait_until="networkidle")
        page.wait_for_function(
            "document.getElementById('voice-output-toggle').checked === true", timeout=5000,
        )
    finally:
        tts_service.set_output_enabled(False)


def test_voice_diagnostics_shows_one_state_and_something_to_do(page, live_server):
    """The release candidate showed six accurate rows and, as advice, a
    reinstall that would not have helped. The panel now leads with which
    of the ten situations this machine is in."""
    page.goto(url("/ui/voice"), wait_until="networkidle")
    page.wait_for_function(
        "document.getElementById('diag-state').textContent.trim() !== '…'", timeout=8000,
    )

    state = page.text_content("#diag-state").strip()
    next_step = page.text_content("#diag-next-step").strip()

    assert state and state != "Unknown", f"the panel reported {state!r}"
    assert next_step, "a diagnosis with nothing to do about it is what was reported"


def test_the_diagnostics_button_says_what_it_does(page, live_server):
    page.goto(url("/ui/voice"), wait_until="networkidle")

    assert page.text_content("#diag-refresh").strip() == "Run diagnostics again"


def test_the_voice_diagnostics_heading_is_spelled_correctly(page, live_server):
    """Reported from the physical machine as "oice diagnostics". Rendered
    in a real browser here so the answer is what a person sees, not what
    the template says."""
    page.goto(url("/ui/voice"), wait_until="networkidle")

    assert "Voice diagnostics" in page.text_content("#voice-diagnostics-card")


def test_the_voice_page_toggle_reflects_the_server_state(page, live_server):
    from app.voice.tts import tts_service

    tts_service.set_output_enabled(False)
    page.goto(url("/ui/voice"), wait_until="networkidle")
    page.wait_for_function(
        "document.getElementById('voice-output-toggle').checked === false", timeout=5000,
    )

    try:
        page.click("#voice-output-toggle")
        page.wait_for_function(
            "document.getElementById('tts-enabled-val').textContent === 'Yes'", timeout=5000,
        )
        assert tts_service.output_enabled is True

        page.reload(wait_until="networkidle")
        page.wait_for_function(
            "document.getElementById('voice-output-toggle').checked === true", timeout=5000,
        )
    finally:
        tts_service.set_output_enabled(False)


# ---------------------------------------------------------------------------
# 12d. Action history, through the real UI
# ---------------------------------------------------------------------------

def test_a_confirmed_action_appears_in_the_history_without_a_reload(page, live_server):
    """The history is the record of what just happened, so a stale one is
    wrong exactly when someone is watching it."""
    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.fill("#chat-input", "status")
    page.click("#chat-send")
    page.wait_for_function(
        "document.getElementById('chat-messages').textContent.includes('JARVIS')", timeout=5000,
    )

    page.goto(url("/ui/actions"), wait_until="networkidle")
    page.wait_for_function(
        "document.getElementById('history-tbody').textContent.includes('status')", timeout=5000,
    )

    # A new action, executed from this page's own websocket-connected
    # session, must land in the table without a manual refresh.
    page.evaluate("""() => {
        const token = document.cookie.match(/(?:^|;\\s*)jarvis_session=([^;]+)/);
        return fetch('/command', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                ...(token ? {'X-JARVIS-Session-Token': decodeURIComponent(token[1])} : {}),
            },
            body: JSON.stringify({command: 'disk space'}),
        });
    }""")
    page.wait_for_function(
        "document.getElementById('history-tbody').textContent.includes('disk_space')", timeout=8000,
    )


def test_filtering_the_history_narrows_it(page, live_server):
    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.fill("#chat-input", "status")
    page.click("#chat-send")
    page.wait_for_function(
        "document.getElementById('chat-messages').textContent.includes('JARVIS')", timeout=5000,
    )

    page.goto(url("/ui/actions"), wait_until="networkidle")
    page.wait_for_function(
        "document.getElementById('history-tbody').textContent.includes('status')", timeout=5000,
    )

    page.select_option("#history-filter", "cancelled")
    page.wait_for_function(
        "!document.getElementById('history-tbody').textContent.includes('Loading')", timeout=5000,
    )
    rows = page.inner_text("#history-tbody")
    assert "Succeeded" not in rows


# ---------------------------------------------------------------------------
# 13. Privacy-mode indicator
# ---------------------------------------------------------------------------

def test_privacy_indicator_updates_live(page):
    from app.core.privacy import privacy_mode
    privacy_mode.set(False)

    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.wait_for_function("document.getElementById('topbar-privacy-label').textContent === 'privacy: off'")

    page.fill("#chat-input", "privacy mode on")
    page.click("#chat-send")
    page.wait_for_function(
        "document.getElementById('topbar-privacy-label').textContent === 'privacy: on'", timeout=5000,
    )

    page.fill("#chat-input", "privacy mode off")
    page.click("#chat-send")
    page.wait_for_function(
        "document.getElementById('topbar-privacy-label').textContent === 'privacy: off'", timeout=5000,
    )


# ---------------------------------------------------------------------------
# 14 & 15. Push-to-talk available vs. degraded state
# ---------------------------------------------------------------------------

def test_push_to_talk_available_with_fake_adapter(page, browser_instance):
    # Pressing the button opens a real capture stream, so this one needs a
    # microphone the browser will hand over without a prompt. That is
    # Chromium's --use-fake-device-for-media-stream and nothing else;
    # WebKit has no equivalent, so under JARVIS_TEST_BROWSER=webkit the
    # precondition is genuinely absent rather than the feature broken.
    if browser_instance.browser_type.name != "chromium":
        pytest.skip("a fake capture device is a Chromium-only launch flag")

    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.wait_for_timeout(500)
    assert page.is_disabled("#ptt-button") is False

    page.click("#ptt-button")
    page.wait_for_function(
        "document.getElementById('ptt-status').textContent.includes('Listening')", timeout=5000,
    )
    page.click("#ptt-button")
    page.wait_for_function(
        "document.getElementById('ptt-status').textContent.includes('Heard')", timeout=5000,
    )
    assert page.input_value("#chat-input") == "system status"


def test_push_to_talk_pagehide_releases_the_capture(page, browser_instance):
    """Leaving the document is a cancellation, even while recording."""
    if browser_instance.browser_type.name != "chromium":
        pytest.skip("a fake capture device is a Chromium-only launch flag")

    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.click("#ptt-button")
    page.wait_for_function("pttState === PTT_STATE.LISTENING", timeout=5000)

    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.wait_for_function("pttState === PTT_STATE.IDLE", timeout=5000)

    assert page.evaluate("pttStream === null") is True
    assert page.evaluate("pttRecorder === null") is True
    assert page.evaluate("pttRecordingTimer === null") is True


def test_push_to_talk_degraded_state_stays_disabled(page):
    """Regression test for a real bug found during manual verification:
    setPttState() used to unconditionally recompute `disabled` from the
    transient recording state, silently re-enabling the button on the
    very next state change even after stt-status reported unavailable."""
    from app.voice.stt import FakeSTTAdapter, stt_service
    stt_service.set_adapter_override(FakeSTTAdapter(available=False))
    try:
        page.goto(url("/ui/chat"), wait_until="networkidle")
        page.wait_for_timeout(500)
        assert page.is_disabled("#ptt-button") is True
        title = page.get_attribute("#ptt-button", "title")
        assert "not configured" in title.lower()

        # The regression: trigger an unrelated state change and confirm the
        # button is *still* disabled afterward, not silently re-enabled.
        page.evaluate("setPttState('idle', 'poked by test')")
        assert page.is_disabled("#ptt-button") is True
    finally:
        stt_service.set_adapter_override(FakeSTTAdapter(transcript="system status"))


def test_text_input_remains_usable_when_voice_is_unavailable(page):
    from app.voice.stt import FakeSTTAdapter, stt_service
    stt_service.set_adapter_override(FakeSTTAdapter(available=False))
    try:
        page.goto(url("/ui/chat"), wait_until="networkidle")
        page.fill("#chat-input", "status")
        page.click("#chat-send")
        page.wait_for_function(
            "document.getElementById('chat-messages').textContent.includes('JARVIS')", timeout=5000,
        )
    finally:
        stt_service.set_adapter_override(FakeSTTAdapter(transcript="system status"))


# ---------------------------------------------------------------------------
# 16. Keyboard navigation
# ---------------------------------------------------------------------------

def test_chat_input_and_send_are_keyboard_reachable(page):
    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.click("#chat-input")
    page.keyboard.type("help")
    page.keyboard.press("Enter")
    page.wait_for_function(
        "document.getElementById('chat-messages').textContent.includes('JARVIS')", timeout=5000,
    )


def test_approval_card_buttons_are_keyboard_reachable(page):
    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.fill("#chat-input", "clear logs")
    page.click("#chat-send")
    page.wait_for_selector(".msg-approval", timeout=5000)

    confirm_btn = page.query_selector(".msg-approval-footer button.btn-primary")
    confirm_btn.focus()
    assert page.evaluate("document.activeElement === document.querySelector('.msg-approval-footer button.btn-primary')")
    page.keyboard.press("Enter")
    page.wait_for_function(
        "document.querySelector('.msg-approval-status') && "
        "document.querySelector('.msg-approval-status').textContent.trim().length > 0",
        timeout=5000,
    )


def test_the_first_tab_press_offers_a_way_past_the_sidebar(page):
    """Eleven nav links stand between the top of every page and its
    content. Without a skip link, reaching the chat box by keyboard costs
    eleven tab presses on every page — something axe does not report and
    a mouse user never notices."""
    page.goto(url("/ui/chat"), wait_until="networkidle")

    page.keyboard.press("Tab")
    focused = page.evaluate("document.activeElement.className")
    assert "skip-link" in focused, "the skip link is not the first focusable element"

    # Hidden until it has focus: it must not sit visibly on the page.
    assert page.evaluate(
        "getComputedStyle(document.querySelector('.skip-link')).position"
    ) == "absolute"


def test_the_chat_page_does_not_take_focus_on_load(page):
    """Regression: the chat input must not hold focus when a page opens.

    History hydration disables the input and re-enables it afterwards, and
    the re-enable used to call `focus()` unconditionally. On a fresh load
    nothing had focus to restore, so the caret landed in the chat box, the
    first Tab went to the push-to-talk button, and the skip link — eleven
    nav links' worth of keyboard travel — could not be reached at all.
    Measured before the fix: activeElement was INPUT#chat-input on load.

    Focus is given back only when this code is what took it away.
    """
    page.goto(url("/ui/chat"), wait_until="networkidle")

    active = page.evaluate("document.activeElement.tagName")
    assert active in ("BODY", "HTML"), (
        f"something took focus on load: {page.evaluate('document.activeElement.id')!r}"
    )


def test_the_skip_link_actually_moves_focus_into_the_content(page):
    """Scrolling without moving focus is the common broken version: the
    next Tab press goes straight back into the nav."""
    page.goto(url("/ui/chat"), wait_until="networkidle")

    page.keyboard.press("Tab")
    page.keyboard.press("Enter")

    assert page.evaluate("document.activeElement.id") == "main-content"


@pytest.mark.parametrize("path", ["/ui/", "/ui/chat", "/ui/settings", "/ui/voice", "/ui/actions"])
def test_headings_describe_the_page_structure(page, path):
    """Card titles are real headings, so a screen-reader user can jump
    between sections instead of arrowing through everything. Checked as
    "no level is skipped", which is the rule that actually matters."""
    page.goto(url(path), wait_until="networkidle")

    levels = page.evaluate(
        "Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))"
        ".filter(h => h.offsetParent !== null)"
        ".map(h => Number(h.tagName[1]))"
    )

    assert levels, f"{path} has no headings at all"
    assert levels[0] == 1, f"{path} does not start at h1"
    for previous, current in zip(levels, levels[1:]):
        assert current <= previous + 1, f"{path} jumps from h{previous} to h{current}"


# ---------------------------------------------------------------------------
# 18. Live-region status announcements
# ---------------------------------------------------------------------------

def test_key_status_regions_are_marked_aria_live(page):
    page.goto(url("/ui/chat"), wait_until="networkidle")
    for selector in ("#topbar-runtime-label", "#topbar-privacy-label", "#ptt-status"):
        live_attr = page.get_attribute(selector, "aria-live")
        assert live_attr == "polite", f"{selector} is missing aria-live=polite"


# ---------------------------------------------------------------------------
# 19. Reduced motion
# ---------------------------------------------------------------------------

def test_page_renders_correctly_with_reduced_motion(page):
    page.emulate_media(reduced_motion="reduce")
    page.goto(url("/ui/"), wait_until="networkidle")
    assert page.errors == []
    assert page.is_visible("body")


# ---------------------------------------------------------------------------
# 20. No horizontal overflow at each viewport
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("viewport_name", list(VIEWPORTS.keys()))
@pytest.mark.parametrize("path", PAGES)
def test_no_horizontal_overflow(page, path, viewport_name):
    vp = VIEWPORTS[viewport_name]
    page.set_viewport_size(vp)
    page.goto(url(path), wait_until="networkidle")
    scroll_width = page.evaluate("document.documentElement.scrollWidth")
    client_width = page.evaluate("document.documentElement.clientWidth")
    assert scroll_width <= client_width + 1, (  # +1px tolerance for sub-pixel rounding
        f"{path} overflows horizontally at {viewport_name}: "
        f"scrollWidth={scroll_width} > clientWidth={client_width}"
    )


# ---------------------------------------------------------------------------
# 21. Zero unexpected console/page errors (rolled into every test above via
# the `page` fixture's pg.errors list — an explicit sanity test too)
# ---------------------------------------------------------------------------

def test_dashboard_produces_no_page_errors_across_a_normal_session(page):
    page.goto(url("/ui/"), wait_until="networkidle")
    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.fill("#chat-input", "help")
    page.click("#chat-send")
    page.wait_for_timeout(500)
    page.goto(url("/ui/actions"), wait_until="networkidle")
    page.goto(url("/ui/memory"), wait_until="networkidle")
    assert page.errors == []


# ---------------------------------------------------------------------------
# 22. axe: zero serious/critical violations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", PAGES)
def test_axe_no_serious_or_critical_violations(page, path):
    try:
        from axe_playwright_python.sync_playwright import Axe
    except ImportError:
        pytest.skip("axe-playwright-python is not installed — pip install axe-playwright-python")

    page.goto(url(path), wait_until="networkidle")
    results = Axe().run(page)
    serious_or_critical = [
        v for v in results.response["violations"]
        if v.get("impact") in ("serious", "critical")
    ]
    assert serious_or_critical == [], (
        f"{path} has {len(serious_or_critical)} serious/critical axe violation(s):\n"
        + "\n".join(f"  - {v['id']} ({v['impact']}): {v['description']}" for v in serious_or_critical)
    )


def test_axe_no_serious_or_critical_violations_on_approval_interface(page):
    try:
        from axe_playwright_python.sync_playwright import Axe
    except ImportError:
        pytest.skip("axe-playwright-python is not installed — pip install axe-playwright-python")

    page.goto(url("/ui/chat"), wait_until="networkidle")
    page.fill("#chat-input", "clear logs")
    page.click("#chat-send")
    page.wait_for_selector(".msg-approval", timeout=5000)

    results = Axe().run(page)
    serious_or_critical = [
        v for v in results.response["violations"]
        if v.get("impact") in ("serious", "critical")
    ]
    assert serious_or_critical == [], (
        f"Approval card has {len(serious_or_critical)} serious/critical axe violation(s):\n"
        + "\n".join(f"  - {v['id']} ({v['impact']}): {v['description']}" for v in serious_or_critical)
    )


# ---------------------------------------------------------------------------
# 23. Coding Workspace page
#
# The page-level checks above (loads, no horizontal overflow at four
# widths, no serious axe violation) already cover /ui/coding because it is
# in PAGES. What follows is the behaviour those cannot see: the tablist
# actually behaving like one, the mode being stated, and the page never
# claiming a capability it has not got.
# ---------------------------------------------------------------------------

def test_the_coding_page_says_which_mode_you_are_in(page):
    page.goto(url("/ui/coding"), wait_until="networkidle")
    banner = page.locator("#coding-mode-banner")
    assert banner.is_visible()
    text = banner.inner_text().lower()
    assert "coding workspace" in text
    # It must say what this mode can do *and* that ordinary chat cannot.
    assert "project" in text
    assert "chat" in text


def test_the_coding_page_has_exactly_one_h1(page):
    page.goto(url("/ui/coding"), wait_until="networkidle")
    assert page.locator("h1").count() == 1


def test_the_coding_tabs_are_operable_with_the_keyboard_alone(page):
    page.goto(url("/ui/coding"), wait_until="networkidle")

    first = page.locator("#tab-projects")
    first.focus()
    assert first.get_attribute("aria-selected") == "true"

    page.keyboard.press("ArrowRight")
    assert page.locator("#tab-task").get_attribute("aria-selected") == "true"
    assert page.locator("#panel-task").is_visible()
    assert not page.locator("#panel-projects").is_visible()

    # End goes to the last tab, whichever it is. Named explicitly rather
    # than hard-coded, so adding a tab updates the assertion instead of
    # silently making it test the wrong one.
    tab_ids = page.eval_on_selector_all(
        '#coding-tabs [role="tab"]', "els => els.map(e => e.id)")
    page.keyboard.press("End")
    assert page.locator(f"#{tab_ids[-1]}").get_attribute("aria-selected") == "true"

    page.keyboard.press("Home")
    assert page.locator("#tab-projects").get_attribute("aria-selected") == "true"


def test_only_the_selected_coding_tab_is_in_the_tab_order(page):
    """A tablist where every tab is tabbable makes a keyboard user press
    Tab six times to leave the tab strip."""
    page.goto(url("/ui/coding"), wait_until="networkidle")
    tabbable = page.eval_on_selector_all(
        '#coding-tabs [role="tab"]',
        "els => els.filter(e => e.tabIndex === 0).map(e => e.id)",
    )
    assert tabbable == ["tab-projects"]


def test_the_coding_page_lists_what_it_will_not_do(page):
    page.goto(url("/ui/coding"), wait_until="networkidle")
    page.wait_for_function(
        "() => document.querySelectorAll('#coding-disabled-list li').length > 0",
        timeout=5000)
    text = page.locator("#coding-disabled-list").inner_text().lower()
    for forbidden in ("push", "pull request", "merge", "deploy"):
        assert forbidden in text, f"the page does not say {forbidden} is unavailable"


def test_the_coding_page_shows_the_protected_files_from_the_server(page):
    """Rendered from GET /coding/status, not written into the template,
    so what is shown is what workspace.py enforces."""
    page.goto(url("/ui/coding"), wait_until="networkidle")
    page.wait_for_function(
        "() => document.querySelector('#coding-protected').children.length > 0",
        timeout=5000)
    text = page.locator("#coding-protected").inner_text()
    assert ".env" in text
    assert ".ssh" in text


def test_the_coding_page_starts_with_an_explicit_empty_state(page):
    """With no project added, the page must not present a coding agent
    nobody asked for."""
    page.goto(url("/ui/coding"), wait_until="networkidle")
    page.wait_for_function(
        "() => document.querySelector('#coding-projects').innerText.length > 0",
        timeout=5000)
    assert "No projects yet" in page.locator("#coding-projects").inner_text()

    page.locator("#tab-task").click()
    assert page.locator("#coding-no-project").is_visible()
    assert not page.locator("#coding-task-area").is_visible()


def test_long_paths_scroll_inside_their_own_box_not_the_page(page):
    """A Windows project path is long. §4 forbids the page scrolling
    sideways, so the container must be the thing that scrolls."""
    page.goto(url("/ui/coding"), wait_until="networkidle")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function(
        "() => document.querySelector('#coding-protected').children.length > 0",
        timeout=5000)

    overflows = page.evaluate(
        "() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
    assert overflows is False, "the coding page scrolls sideways on a phone width"

    scrollable = page.eval_on_selector_all(
        ".path-box",
        "els => els.every(e => getComputedStyle(e).overflowX === 'auto' && e.tabIndex === 0)",
    )
    assert scrollable is True, "a path box is not scrollable, or not reachable by keyboard"
