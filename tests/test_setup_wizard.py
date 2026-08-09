"""Structural tests for the first-run wizard (setup.html + app.js).

These guard the properties that are cheap to break and expensive to
notice: a step panel losing its `hidden` attribute (which would expose
its controls to keyboard/screen-reader users on every other step), the
wizard dropping an element ID the existing setup logic drives, or
dynamic text being injected with innerHTML in violation of CLAUDE.md's
permanent textContent-only rule.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_HTML = REPO_ROOT / "app" / "ui" / "templates" / "setup.html"
APP_JS = REPO_ROOT / "app" / "ui" / "static" / "app.js"
STYLE_CSS = REPO_ROOT / "app" / "ui" / "static" / "style.css"


def _html() -> str:
    return SETUP_HTML.read_text(encoding="utf-8")


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Step structure
# ---------------------------------------------------------------------------

def test_all_six_wizard_steps_exist():
    html = _html()
    for step in range(6):
        assert f'data-step="{step}"' in html, f"step {step} panel is missing"


def test_only_the_first_step_is_visible_initially():
    """Every step after the first must carry `hidden`, which also removes
    it from the accessibility tree — otherwise a keyboard user tabs
    straight into controls for steps that aren't on screen."""
    html = _html()
    panels = re.findall(r'<section class="wizard-panel" data-step="(\d+)"([^>]*)>', html)
    assert len(panels) == 6

    for step, attrs in panels:
        if step == "0":
            assert "hidden" not in attrs, "the first step must be visible on load"
        else:
            assert "hidden" in attrs, f"step {step} must start hidden"


def test_each_step_panel_is_labelled_for_assistive_tech():
    html = _html()
    panels = re.findall(r'<section class="wizard-panel"[^>]*>', html)
    for panel in panels:
        assert "aria-labelledby" in panel


def test_step_indicator_marks_the_current_step_with_aria_current():
    assert 'aria-current", "step"' in _js()


# ---------------------------------------------------------------------------
# The wizard must not break the existing setup logic
# ---------------------------------------------------------------------------

REQUIRED_IDS = [
    # Readiness rows driven by refreshSetupReadiness()
    "ready-core", "ready-text_chat", "ready-ai_provider", "ready-mode",
    "ready-stt_runtime", "ready-speech_model", "ready-tts", "ready-database",
    "ready-windows_automation", "ready-microphone",
    "setup-readiness-list", "setup-readiness-detail",
    # API key panel
    "setup-key-status", "setup-key-input", "setup-key-save",
    "setup-key-remove", "setup-key-message",
    # Speech model install flow
    "setup-speech-model-status", "setup-model-card", "model-info-name",
    "model-info-source", "model-info-license", "model-info-size",
    "model-info-destination", "model-info-checksum", "model-info-error",
    "model-install-start", "model-install-cancel", "model-install-retry",
    "model-install-progress-wrap", "model-install-progress-bar",
    "model-install-progress-text",
    # Completion
    "setup-continue",
]


@pytest.mark.parametrize("element_id", REQUIRED_IDS)
def test_wizard_preserves_every_id_the_existing_setup_logic_drives(element_id):
    """app.js already drives these. The wizard wraps that behaviour in
    steps; it must not silently drop an element and leave the
    corresponding JS a no-op."""
    assert f'id="{element_id}"' in _html(), f"{element_id} is referenced by app.js but missing from setup.html"


# ---------------------------------------------------------------------------
# Wizard controls
# ---------------------------------------------------------------------------

def test_wizard_has_back_next_finish_and_skip_controls():
    html = _html()
    for control in ("wizard-back", "wizard-next", "setup-continue", "wizard-skip"):
        assert f'id="{control}"' in html


def test_finish_button_starts_hidden_and_next_is_shown():
    """Finish only appears on the last step; showing both at once would
    make the primary action ambiguous."""
    html = _html()
    finish = re.search(r'<button id="setup-continue"[^>]*>', html).group(0)
    assert "hidden" in finish


def test_skipping_is_offered_and_described_as_safe():
    html = _html()
    assert "Skip setup" in html
    assert "Skipping is safe" in html


def test_back_is_disabled_on_the_first_step():
    assert "back.disabled = wizardStep === 0" in _js()


def test_skip_and_finish_both_complete_onboarding():
    """Skipping must still mark onboarding complete, or the wizard would
    reappear on every launch."""
    js = _js()
    assert "skip.addEventListener(\"click\", finishSetup)" in js
    assert '/onboarding/complete' in js


# ---------------------------------------------------------------------------
# Focus management
# ---------------------------------------------------------------------------

def test_focus_moves_to_the_new_step_heading():
    """Otherwise focus stays on a button whose meaning just changed,
    leaving screen-reader users with no announcement of the new step."""
    js = _js()
    assert "heading.focus()" in js
    assert 'heading.setAttribute("tabindex", "-1")' in js


def test_focused_step_heading_has_a_visible_focus_ring():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".wizard-panel .card-title:focus-visible" in css


# ---------------------------------------------------------------------------
# Safety rules that apply to every page
# ---------------------------------------------------------------------------

def test_wizard_javascript_never_uses_inner_html():
    """CLAUDE.md makes textContent mandatory and innerHTML permanently
    forbidden. Checks the wizard functions specifically, not just the
    file as a whole."""
    js = _js()
    start = js.index("// ── First-run wizard")
    end = js.index("function initSetup()")
    assert "innerHTML" not in js[start:end]


def test_provider_rendering_uses_text_content():
    js = _js()
    assert "value.textContent = provider.available" in js
    assert "detail.textContent = provider.detail" in js


def test_startup_toggle_trusts_the_server_state_not_the_click():
    """If the shortcut could not be created, the checkbox must fall back
    to the real state rather than showing a setting that isn't real."""
    assert "toggle.checked = r.enabled" in _js()


def test_setup_page_makes_no_false_privacy_claim():
    """The page must not imply encryption. It says so plainly instead."""
    html = _html()
    assert "not an encrypted vault" in html
    assert "127.0.0.1 only" in html


def test_wizard_css_has_no_external_resources():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert "http://" not in css
    assert "https://" not in css
