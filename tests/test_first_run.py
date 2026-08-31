"""Structural tests for the first-run screen (setup.html + app.js).

Real hardware testing rejected the six-step wizard this replaced: it
exposed provider discovery, speech-runtime preparation and a readiness
table full of "Not ready" rows to someone who had just double-clicked an
installer. First run now asks two questions — what to call you, and your
API key — and everything else moved to the page that owns it.

These tests guard that shape. A test file that only asserted the two
fields exist would not notice the wizard creeping back one card at a
time, so the ceiling is asserted too.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "app" / "ui" / "templates"
SETUP_HTML = TEMPLATES / "setup.html"
SETTINGS_HTML = TEMPLATES / "settings.html"
VOICE_HTML = TEMPLATES / "voice.html"
APP_JS = REPO_ROOT / "app" / "ui" / "static" / "app.js"
STYLE_CSS = REPO_ROOT / "app" / "ui" / "static" / "style.css"


def _html() -> str:
    return SETUP_HTML.read_text(encoding="utf-8")


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# What first run asks
# ---------------------------------------------------------------------------

#: Every input first run is allowed to contain, by id. Adding to this
#: list is a product decision about what someone must face before they
#: have a working assistant — not a convenience.
ALLOWED_SETUP_INPUTS = (
    "setup-name-input",
    "setup-key-input",
    # Raised from two to three by the owner's instruction after a real-PC
    # failure: an Anthropic *identity-linked* key (a personal or service
    # account key not scoped to one workspace) is rejected on every
    # request without a Workspace ID, so a first run that cannot accept
    # one is a first run that cannot finish for those keys. Optional, and
    # correctly left blank for a legacy workspace-scoped key — see
    # app/core/ai/workspace.py.
    "setup-workspace-input",
)


def test_first_run_asks_for_a_preferred_name_and_an_api_key():
    html = _html()
    assert 'id="setup-name-input"' in html
    assert 'id="setup-key-input"' in html


def test_first_run_asks_for_nothing_else():
    """The ceiling, not the floor. Every input on this page is one more
    thing between someone and a working assistant, and the six-step
    version is what the owner rejected.

    The count alone was the guard before; it is now the count *and* the
    identity of each field, which is strictly harder to creep past — a
    fourth card cannot arrive by swapping one input for another.
    """
    html = _html()
    inputs = re.findall(r"<(?:input|select|textarea)\b[^>]*>", html)
    assert len(inputs) == len(ALLOWED_SETUP_INPUTS), (
        f"first run must ask exactly {len(ALLOWED_SETUP_INPUTS)} things, found {len(inputs)}"
    )
    for field_id in ALLOWED_SETUP_INPUTS:
        assert f'id="{field_id}"' in html, f"first run lost its {field_id!r} field"


def test_the_only_optional_first_run_field_is_the_workspace_id():
    """The name and the key are what first run is *for*. Anything else on
    this page has to justify itself, and this pins the one that did."""
    html = _html()
    at = html.index('id="setup-workspace-input"')
    # A window either side: the "(optional)" marker is in the <label> that
    # precedes the input, the placeholder is on the input itself, and the
    # hint follows it.
    block = html[max(0, at - 400):at + 800].lower()
    assert "optional" in block
    assert "wrkspc_" in block
    assert "claude console" in block


def test_first_run_is_a_single_screen():
    """No steps, no progress indicator, no back button. A progress
    indicator on two fields makes them look like the start of something
    longer."""
    html = _html()
    for gone in ("wizard-step", "data-step", "wizard-back", "wizard-next", "wizard-skip"):
        assert gone not in html, f"{gone} belongs to the wizard that was removed"


def test_neither_field_is_required_to_continue():
    """Advanced configuration must not block first launch — and neither
    must basic configuration. JARVIS runs commands, opens apps and takes
    notes with no key at all."""
    html = _html()
    assert 'id="setup-skip"' in html
    assert "Optional" in html
    assert "Without a key" in html


def test_the_name_field_is_labelled_and_bounded():
    html = _html()
    field = re.search(r'<input id="setup-name-input"[^>]*>', html).group(0)
    assert 'maxlength="40"' in field
    assert '<label class="form-label" for="setup-name-input">' in html


def test_the_key_field_is_masked_and_labelled():
    html = _html()
    field = re.search(r'<input id="setup-key-input"[^>]*>', html).group(0)
    assert 'type="password"' in field
    assert 'autocomplete="off"' in field
    assert '<label class="form-label" for="setup-key-input">' in html


def test_the_page_never_renders_a_key():
    """CLAUDE.md's Phase 4 rule: no template may render ANTHROPIC_API_KEY
    or an sk- token. The placeholder is a shape, not a value."""
    html = _html()
    assert "ANTHROPIC_API_KEY" not in html
    assert "sk-ant" not in html, "not even a key-shaped placeholder belongs in a template"


# ---------------------------------------------------------------------------
# What it no longer asks — and where that went instead
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("removed", [
    "wizard-provider-list",      # provider discovery
    "wizard-startup-toggle",     # start with Windows
    "wizard-close-action",       # close behaviour (and it was wired to nothing)
    "setup-readiness-list",      # the wall of "Not ready"
    "model-install-start",       # speech-model download
])
def test_setup_work_is_no_longer_on_the_first_run_screen(removed):
    assert removed not in _html()


def test_the_speech_model_installer_moved_to_the_voice_page():
    """Removed from first run, not removed from the product — the
    capability has to still be reachable."""
    voice = VOICE_HTML.read_text(encoding="utf-8")
    for element_id in (
        "setup-model-card", "model-info-name", "model-info-source",
        "model-info-license", "model-info-size", "model-info-destination",
        "model-info-checksum", "model-info-error", "model-install-start",
        "model-install-cancel", "model-install-retry",
        "model-install-progress-wrap", "model-install-progress-bar",
        "model-install-progress-text",
    ):
        assert f'id="{element_id}"' in voice, f"{element_id} was dropped, not moved"


def test_the_voice_page_wires_the_installer_it_now_owns():
    """Moving the markup without the handlers would leave three dead
    buttons."""
    js = _js()
    init_voice = js[js.index("function initVoice()"):js.index("// ── Actions ─")]
    assert 'modelStartBtn.addEventListener("click", startModelInstall)' in init_voice
    assert 'modelCancelBtn.addEventListener("click", cancelModelInstall)' in init_voice
    assert "refreshModelPreview()" in init_voice
    assert "pollModelInstallStatus()" in init_voice


def test_preferences_moved_to_settings():
    settings = SETTINGS_HTML.read_text(encoding="utf-8")
    assert 'id="settings-name-input"' in settings
    assert 'id="settings-close-action"' in settings
    assert 'id="settings-startup-toggle"' in settings


def test_the_close_action_control_is_actually_wired_now():
    """It existed on the old setup screen and was connected to nothing at
    all — a control that silently does nothing is worse than no control."""
    js = _js()
    assert '"/settings/close-action"' in js
    assert "refreshSettingsCloseAction" in js


def test_the_first_run_page_points_at_where_the_rest_lives():
    html = _html()
    assert "/ui/voice" in html
    assert "/ui/settings" in html


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------

def test_finishing_and_skipping_both_complete_onboarding():
    """Otherwise the setup screen reappears on every launch."""
    js = _js()
    assert 'skipBtn.addEventListener("click", finishSetup)' in js
    assert "/onboarding/complete" in js


def test_setup_has_an_assertive_place_for_persistence_failures():
    html = _html()
    assert 'id="setup-finish-message"' in html
    assert 'role="alert"' in html
    assert 'aria-live="assertive"' in html


def test_the_name_is_saved_before_leaving_the_page():
    js = _js()
    init_setup = js[js.index("function initSetup()"):]
    assert 'savePreferredName("setup-name-input")' in init_setup


def test_a_name_save_failure_stays_on_setup_and_says_why():
    js = _js()
    init_setup = js[js.index("function initSetup()"):js.index("function initVoice()")]
    assert "const nameSaved = await savePreferredName" in init_setup
    assert "if (!nameSaved)" in init_setup
    assert "could not save your name" in init_setup
    assert init_setup.index("if (!nameSaved)") < init_setup.index("await finishSetup()")


def test_a_completion_failure_does_not_redirect_or_claim_success():
    js = _js()
    finish = js[js.index("async function finishSetup()"):js.index("function initSetup()")]
    assert "if (!result.success)" in finish
    assert "could not finish setup" in finish
    assert 'window.location.href = "/ui/"' in finish
    assert finish.index('window.location.href = "/ui/"') < finish.index("} catch (e)")
    assert "return false" in finish


def test_a_rejected_key_keeps_the_user_on_the_page_to_fix_it():
    js = _js()
    assert "if (!ok && stillUnconfigured) return;" in js


def test_a_stored_key_clears_the_field_and_a_rejected_one_does_not():
    """Leaving a rejected key in the box is what lets someone fix a typo
    instead of retyping the whole thing."""
    assert "if (r.stored && input) input.value = \"\";" in _js()


def test_the_save_button_says_it_is_working():
    """Verifying a key is a real network round trip. A button that just
    stops responding reads as a crash."""
    js = _js()
    assert 'button.textContent = "Checking…"' in js


def test_the_field_shows_what_was_actually_stored():
    """The server sanitises the name; a field that keeps showing
    something different would be a lie."""
    js = _js()
    assert "input.value = r.name" in js


def test_settings_and_first_run_share_one_key_save_path():
    """A key rejected during setup and the same key rejected in Settings
    must say the same thing."""
    js = _js()
    assert 'saveApiKeyFrom("setup-key-input"' in js
    assert 'saveApiKeyFrom("settings-key-input"' in js


# ---------------------------------------------------------------------------
# Rules that apply to every page
# ---------------------------------------------------------------------------

def test_first_run_javascript_never_uses_inner_html():
    """CLAUDE.md makes textContent mandatory and innerHTML permanently
    forbidden. Checked over the first-run functions specifically, not
    just the file as a whole."""
    js = _js()
    start = js.index("// ── First run ─")
    end = js.index("function initVoice()")
    assert "innerHTML" not in js[start:end]


def test_first_run_css_has_no_external_resources():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert "http://" not in css
    assert "https://" not in css


def test_the_privacy_claim_that_used_to_live_here_is_still_made_somewhere():
    """Dropping the privacy step must not drop the honesty. Settings
    still says plainly that the local database is not encrypted."""
    settings = SETTINGS_HTML.read_text(encoding="utf-8")
    assert "not an encrypted vault" in settings


def test_first_run_still_says_where_data_goes():
    html = _html()
    assert "Nothing leaves this machine" in html
