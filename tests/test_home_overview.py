"""Tests for the Home / Overview page.

The requirement this page has to meet is that a user can see, at a
glance, what JARVIS is doing and whether anything needs them. So the
tests focus on two things: every required panel is present, and every
panel has an explicit empty state and an explicit failure state — a
dashboard that silently renders nothing is indistinguishable from one
that is broken.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import app

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "app" / "ui" / "templates" / "dashboard.html"
APP_JS = REPO_ROOT / "app" / "ui" / "static" / "app.js"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _overview_js() -> str:
    js = _js()
    return js[js.index("// ── Home / Overview"):js.index("// ── Settings page")]


# ---------------------------------------------------------------------------
# Required panels
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("element_id", [
    "dash-runtime-state",     # what JARVIS is doing right now
    "dash-provider",          # AI provider status
    "dash-voice-input",       # microphone / speech status
    "dash-privacy",           # privacy mode
    "dash-pending-approvals",  # pending approvals
    "dash-recent-actions",    # recent actions
])
def test_overview_panel_is_present(client, element_id):
    assert f'id="{element_id}"' in client.get("/ui/").text


@pytest.mark.parametrize("href", ["/ui/chat", "/ui/voice", "/ui/actions", "/ui/settings", "/ui/diagnostics"])
def test_overview_offers_a_shortcut(client, href):
    body = client.get("/ui/").text
    assert f'href="{href}"' in body


def test_shortcuts_are_marked_up_as_navigation(client):
    """A row of links is navigation, not decoration — it gets a label so
    screen-reader users can find and skip it."""
    assert 'aria-label="Quick links"' in client.get("/ui/").text


def test_live_regions_announce_changing_state(client):
    """Runtime state, privacy and pending approvals change while the page
    is open; each must announce rather than change silently."""
    body = client.get("/ui/").text
    for element_id in ("dash-runtime-state", "dash-privacy", "dash-pending-approvals"):
        index = body.index(f'id="{element_id}"')
        window = body[max(0, index - 200):index + 200]
        assert "aria-live" in window, f"{element_id} must be a live region"


# ---------------------------------------------------------------------------
# Empty and failure states — the property that makes the page trustworthy
# ---------------------------------------------------------------------------

def test_pending_approvals_has_an_explicit_empty_state():
    assert "Nothing is waiting for approval." in _overview_js()


def test_recent_actions_has_an_explicit_empty_state():
    assert "No actions yet." in _overview_js()


@pytest.mark.parametrize("failure_text", [
    "Could not check pending approvals.",
    "Could not load recent actions.",
])
def test_panel_reports_its_own_failure(failure_text):
    """Distinguishes "nothing to show" from "could not load" — the two
    look identical otherwise, and only one of them is fine."""
    assert failure_text in _overview_js()


def test_provider_and_voice_panels_have_failure_states():
    js = _overview_js()
    assert js.count('"Unavailable"') >= 2


def test_local_only_state_explains_what_still_works():
    """With no provider configured the page must not read as broken —
    deterministic commands still work."""
    js = _overview_js()
    assert "Local only" in js
    assert "Commands work" in js


# ---------------------------------------------------------------------------
# Liveness and consistency
# ---------------------------------------------------------------------------

def test_runtime_card_reads_the_same_source_as_the_topbar():
    """Two indicators of the same thing that can disagree are worse than
    one, so there is exactly one function that writes either.

    This used to read the first 400 characters after the `runtime_state`
    branch and look for `dash-runtime-state` in them, which held the
    three calls adjacent by making a comment between them a test failure.
    The calls now live together inside `applyRuntimeObservation`, which
    the event handler delegates to — a stronger version of the same
    property, since a snapshot cannot bypass it either.

    `tests/test_runtime_ordering.py` proves the behaviour in a browser;
    this pins the structure that makes it impossible to get wrong.
    """
    js = _js()
    handler = js[js.index('if (evt.type === "runtime_state"'):]
    assert "applyRuntimeObservation" in handler[:300]

    applier = js[js.index("function applyRuntimeObservation"):]
    applier = applier[:applier.index("\n}")]
    for written in ("setRuntimeLabel", "setRuntimeCore", "dash-runtime-state"):
        assert written in applier, f"{written} is no longer written by the one applier"

    # Exactly three mentions of setRuntimeCore in the whole file: its own
    # definition, the applier, and runtimeConnectionLost. A fourth is a
    # second writer, which is how the topbar came to read STANDBY beside
    # a core already saying the stream was down.
    assert js.count("setRuntimeCore(") == 3, (
        "setRuntimeCore is written from somewhere new — the core has a "
        "second writer again"
    )
    assert "setRuntimeCore(" in js[js.index("function runtimeConnectionLost"):][:600]

    # The card has one other writer, and it copies the topbar's text
    # rather than deciding anything, so the two cannot disagree.
    fallback = js[js.index("async function refreshOverviewRuntimeState"):]
    fallback = fallback[:fallback.index("\n}")]
    assert "topbar-runtime-label" in fallback and "label.textContent" in fallback


def test_approvals_panel_refreshes_when_an_action_changes():
    """A stale "nothing waiting" is worse than a short delay."""
    js = _js()
    assert "refreshOverviewApprovals();" in js
    assert 'if ($("dash-pending-approvals"))' in js


def test_runtime_card_is_seeded_on_load_not_left_blank():
    """Otherwise it shows an ellipsis until the first WebSocket event,
    which may be a long time on an idle system."""
    assert "refreshOverviewRuntimeState" in _js()


# ---------------------------------------------------------------------------
# Safety rules
# ---------------------------------------------------------------------------

def test_overview_javascript_never_uses_inner_html():
    assert "innerHTML" not in _overview_js()


def test_overview_states_push_to_talk_is_not_always_listening(client):
    """The page must not imply continuous listening exists."""
    assert "never always listening" in _overview_js()
