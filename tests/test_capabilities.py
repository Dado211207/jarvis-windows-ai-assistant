"""The model has to know what it is.

Reproduced from a real session on the installed release candidate:

    User:   answer me with your voice
    JARVIS: I appreciate the request, Dado, but I don't have
            text-to-speech capabilities built into this interface right
            now. I can only communicate through text.

and then it recommended Windows Narrator, NaturalReader and Google Docs
— three other people's programs, for a job it can do itself. Nothing was
broken but the prompt: JARVIS has a complete local TTS stack and the
model was never told, so when asked about itself it answered from
general knowledge about chat interfaces.

These tests hold the fix in place at the seam where it actually
happened: the text handed to the provider. What the model then says with
that text is not something a test can assert, but "the sentence that
would have made it answer correctly is present" is, and its absence is
exactly what shipped.
"""

import pytest

from app.core import capabilities
from app.core.capabilities import Capabilities, describe_for_prompt
from app.core.system_prompt import SYSTEM_PROMPT, build_system_prompt

# The three programs JARVIS actually recommended in the reported session.
RECOMMENDED_INSTEAD = ("Narrator", "NaturalReader", "Google Docs")


def _state(**overrides) -> Capabilities:
    """A speaking, ready installation unless a test says otherwise."""
    base = dict(
        speech_output_ready=True,
        speech_output_engine="JARVIS neural voice",
        speech_output_detail="Ready — speaking as George.",
        speaks_replies=False,
        voice_name="George",
        speech_input_ready=True,
        speech_input_detail="Ready.",
        local_ai_ready=False,
        local_ai_detail="Local AI is not set up on this computer.",
        desktop_actions=True,
    )
    base.update(overrides)
    return Capabilities(**base)


# ---------------------------------------------------------------------------
# The defect itself
# ---------------------------------------------------------------------------

def test_a_working_voice_is_stated_as_working():
    """The one fact whose absence produced the reported answer."""
    block = describe_for_prompt(_state())

    assert "Text-to-speech: WORKING" in block
    assert "JARVIS neural voice" in block
    assert "George" in block


def test_the_prompt_forbids_sending_the_user_to_another_program():
    """Not a general "be helpful" instruction — the three names it
    actually offered, so a regression is caught by the same evidence
    that reported it."""
    for program in RECOMMENDED_INSTEAD:
        assert program in SYSTEM_PROMPT, (
            f"the prompt must name {program} as something never to recommend"
        )


def test_the_capability_block_reaches_the_provider(monkeypatch):
    """The block existing is not the fix; the block being in the text
    handed to the model is."""
    from app.core.brain import Brain

    monkeypatch.setattr(capabilities, "describe_now", lambda: "Your current capabilities on this machine:\n- Text-to-speech: WORKING.")

    prompt = Brain._system_prompt()

    assert "Your current capabilities on this machine:" in prompt
    assert "Text-to-speech: WORKING." in prompt


def test_the_real_snapshot_reaches_the_provider_too():
    """Guards the wiring rather than the monkeypatch above: with nothing
    faked, the prompt must still carry a generated capability block. On
    this Linux test machine every capability reports unavailable, which
    is the correct answer and still an answer."""
    from app.core.brain import Brain

    prompt = Brain._system_prompt()

    assert "Your current capabilities on this machine:" in prompt
    assert "Text-to-speech:" in prompt
    assert "Voice input (push-to-talk):" in prompt
    assert "Local AI" in prompt


# ---------------------------------------------------------------------------
# An unavailable capability is a diagnosis, not a shrug
# ---------------------------------------------------------------------------

def test_an_unusable_voice_still_says_the_voice_exists():
    """"I don't have text-to-speech" was wrong even while speech was
    broken. The product has a voice; it could not run."""
    block = describe_for_prompt(_state(
        speech_output_ready=False,
        speech_output_detail="The neural voice is not installed yet. Install it from the Voice page.",
    ))

    assert "NOT USABLE RIGHT NOW" in block
    assert "You do have a built-in voice" in block
    assert "Install it from the Voice page" in block, "the fix must travel with the cause"


def test_an_unusable_voice_does_not_invite_a_third_party_suggestion():
    block = describe_for_prompt(_state(speech_output_ready=False))

    assert "Do not send the user to another program." in block


def test_the_speak_every_reply_switch_is_reported_either_way():
    """CLAUDE.md's Phase 3 rule: one flag decides whether JARVIS speaks.
    The model has to be told which way it is set, or it will guess — and
    "speaking is off" must not be reported as "speech is impossible"."""
    on = describe_for_prompt(_state(speaks_replies=True))
    off = describe_for_prompt(_state(speaks_replies=False))

    assert "Speaking every reply aloud is currently ON" in on
    assert "Speaking every reply aloud is currently OFF" in off
    assert "you can still speak a single answer on request" in off


@pytest.mark.parametrize("ready,expected", [(True, "AVAILABLE"), (False, "NOT AVAILABLE")])
def test_voice_input_state_is_reported(ready, expected):
    block = describe_for_prompt(_state(speech_input_ready=ready, speech_input_detail="detail here"))

    assert f"Voice input (push-to-talk): {expected}" in block
    assert "detail here" in block


@pytest.mark.parametrize("ready,expected", [(True, "READY"), (False, "NOT READY")])
def test_local_ai_state_is_reported(ready, expected):
    block = describe_for_prompt(_state(local_ai_ready=ready, local_ai_detail="detail here"))

    assert f"Local AI (Ollama, offline): {expected}" in block
    assert "detail here" in block


def test_desktop_actions_are_stated():
    assert "Safe Windows actions: AVAILABLE" in describe_for_prompt(_state())


# ---------------------------------------------------------------------------
# It is computed, not remembered
# ---------------------------------------------------------------------------

def test_the_snapshot_is_taken_fresh_every_time(monkeypatch):
    """A voice that finished installing two minutes ago is one the model
    has to know it has. Caching would make the answer wrong in exactly
    the moment somebody asks."""
    calls = []

    def _statuses(voice_key):
        calls.append(voice_key)
        return []

    from app.voice import engines

    monkeypatch.setattr(engines, "statuses", _statuses)

    capabilities.snapshot()
    capabilities.snapshot()

    assert len(calls) == 2, "the engine state must be re-read, not remembered"


def test_the_snapshot_reads_the_one_speech_flag(monkeypatch):
    """Two flags is how the desktop app ended up never speaking at all."""
    from app.voice.tts import tts_service

    monkeypatch.setattr(type(tts_service), "output_enabled", property(lambda self: True))
    assert capabilities.snapshot().speaks_replies is True

    monkeypatch.setattr(type(tts_service), "output_enabled", property(lambda self: False))
    assert capabilities.snapshot().speaks_replies is False


# ---------------------------------------------------------------------------
# It cannot break a conversation, and cannot carry an instruction
# ---------------------------------------------------------------------------

def test_a_capability_that_raises_is_reported_as_absent_not_propagated(monkeypatch):
    """A user asking a question is not the moment to raise because a
    voice file is unreadable. Unknown reads as unavailable, which is the
    honest answer and the cautious one."""
    from app.voice import engines

    def _boom(*_args, **_kwargs):
        raise OSError("the model file is unreadable")

    monkeypatch.setattr(engines, "statuses", _boom)
    monkeypatch.setattr(engines, "unavailable_message", _boom)

    state = capabilities.snapshot()

    assert state.speech_output_ready is False
    assert state.speech_output_detail  # something was still said


def test_describe_now_never_raises(monkeypatch):
    from app.core import local_ai
    from app.voice import engines, stt

    def _boom(*_args, **_kwargs):
        raise RuntimeError("everything is broken")

    monkeypatch.setattr(engines, "statuses", _boom)
    monkeypatch.setattr(engines, "selected_voice_key", _boom)
    monkeypatch.setattr(engines, "unavailable_message", _boom)
    monkeypatch.setattr(local_ai, "describe", _boom)
    monkeypatch.setattr(stt.stt_service, "runtime_status", _boom)

    block = capabilities.describe_now()

    assert "Your current capabilities on this machine:" in block


def test_the_block_contains_no_user_supplied_text(monkeypatch):
    """Everything here is generated from booleans and a fixed set of
    engine names, so the block cannot carry an injected instruction into
    the prompt. The one piece of user text that does reach the prompt —
    the preferred name — is sanitised by system_prompt.py and stays that
    module's problem."""
    from app.core import preferences
    from app.core.brain import Brain

    preferences.store("preferred_name", "Bob")
    prompt = Brain._system_prompt()
    block = prompt.split("Your current capabilities on this machine:", 1)[1]

    assert "Bob" not in block


# ---------------------------------------------------------------------------
# The prompt is still only ever appended to
# ---------------------------------------------------------------------------

def test_capabilities_are_appended_never_substituted():
    prompt = build_system_prompt("Dado", capabilities="Your current capabilities on this machine:\n- x")

    assert prompt.startswith(SYSTEM_PROMPT)
    assert 'Address the user as "Dado"' in prompt
    assert "Your current capabilities on this machine:" in prompt


def test_no_capabilities_leaves_the_prompt_exactly_as_it_was():
    assert build_system_prompt("") == SYSTEM_PROMPT
    assert build_system_prompt("", capabilities="") == SYSTEM_PROMPT


def test_the_capability_block_comes_after_the_rules_it_is_read_against():
    prompt = build_system_prompt("Dado", capabilities="CAPS")

    assert prompt.index("CAPS") > prompt.index('Address the user as "Dado"')
