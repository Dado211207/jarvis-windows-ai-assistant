"""Asking JARVIS to speak must reach the speech engine, not the model.

The reported session, verbatim:

    User:   answer me with your voice
    JARVIS: I appreciate the request, Dado, but I don't have
            text-to-speech capabilities built into this interface right
            now. I can only communicate through text.

followed by recommendations for Windows Narrator, NaturalReader and
Google Docs. tests/test_capabilities.py covers the model no longer
believing that. This file covers the sharper fix: the request never
reaches the model at all, because CLAUDE.md's Phase 2 rule says
deterministic routes win and a request to use a shipped capability
should never have been a guess.

No real audio is played anywhere here — the speech service is mocked
throughout, per CLAUDE.md's Phase 3 testing rule.
"""

import ast
import io
import inspect
import tokenize
from unittest.mock import MagicMock, patch

import pytest

from app.core.router import find_route
from app.voice import speak_reply


def _source_without_prose() -> str:
    """What the module *does*, with comments and docstrings removed.

    The same false-positive class tests/test_clean_install_script.py
    documents on its own `_code_only()` helper: prose explaining why
    something is deliberately never done contains the very words a naive
    substring search is looking for. This module's docstring names all
    three programs it must never recommend, for exactly that reason.
    """
    source = inspect.getsource(speak_reply)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                source = source.replace(docstring, "")
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return "\n".join(tok.string for tok in tokens if tok.type != tokenize.COMMENT)

# Every phrasing that must reach the speech engine. The first is the one
# from the reported session; the rest are the neighbours a person tries
# next when the first appears not to work.
SPEAK_INTENTS = [
    "answer me with your voice",
    "answer with your voice",
    "reply with your voice",
    "respond with your voice",
    "talk to me with your voice",
    "speak to me with your voice",
    "answer me using your voice",
    "answer me in your voice",
    "use your voice",
    "say that",
    "say that again",
    "say it again",
    "say that out loud",
    "say that aloud",
    "read that aloud",
    "read this aloud",
    "read that out loud",
    "read it back to me",
    "speak that",
    "speak this aloud",
    "repeat that out loud",
    "say your last answer",
    "read the previous reply aloud",
    "speak",
    "speak out loud",
    "speak aloud",
]


@pytest.fixture
def speaking_service():
    """A speech service that can speak and records what it was given."""
    service = MagicMock()
    service.is_available.return_value = True
    service.voice_key = "bm_george"
    service.output_enabled = True
    service.active_engine.return_value = "kokoro"
    service.speak.return_value = MagicMock(success=True, message="Speaking: 'x'")
    with patch("app.voice.tts.tts_service", service):
        yield service


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", SPEAK_INTENTS)
def test_every_way_of_asking_reaches_the_speech_tool(phrase):
    match = find_route(phrase)

    assert match is not None, f"{phrase!r} fell through to the AI instead of the speech engine"
    assert match[0].tool_name == "speak_last_reply", f"{phrase!r} routed to {match[0].tool_name}"


@pytest.mark.parametrize("phrase", ["ANSWER ME WITH YOUR VOICE", "Say That Again", "  read this aloud  "])
def test_case_and_surrounding_space_do_not_matter(phrase):
    assert find_route(phrase)[0].tool_name == "speak_last_reply"


@pytest.mark.parametrize("phrase,tool", [
    ("speak on", "tts_enable"),
    ("speak off", "tts_disable"),
    ("speak status", "tts_status"),
    ("speak test", "tts_test"),
    ("stop speaking", "tts_stop"),
])
def test_the_existing_speech_commands_are_not_shadowed(phrase, tool):
    """The new patterns sit after these four in ROUTES. Order is the only
    thing keeping "speak on" from being read as "speak, on"."""
    assert find_route(phrase)[0].tool_name == tool


@pytest.mark.parametrize("phrase", [
    "what is the weather",
    "say hello to my friend",
    "read note shopping",
    "speak french",
    "tell me about voice synthesis",
])
def test_ordinary_requests_still_go_to_the_ai(phrase):
    """Over-matching here would be its own defect: a question about
    speech is not a request to speak."""
    match = find_route(phrase)

    assert match is None or match[0].tool_name != "speak_last_reply"


def test_the_tool_is_registered_under_the_name_the_route_uses():
    from app.core.brain import brain
    from app.core.tool_registry import registry

    brain.initialise()

    assert registry.get("speak_last_reply") is not None


# ---------------------------------------------------------------------------
# What it speaks
# ---------------------------------------------------------------------------

def test_it_speaks_the_previous_answer_verbatim(speaking_service, monkeypatch):
    """"Say that" means *that*. Regenerating an answer to speak would
    make the spoken version and the written one disagree."""
    monkeypatch.setattr(speak_reply, "last_assistant_reply", lambda: "The disk is 62% full.")

    result = speak_reply._speak_last_reply()

    assert result["success"] is True
    speaking_service.speak.assert_called_once_with("The disk is 62% full.")


def test_a_long_answer_is_read_from_the_start_rather_than_refused(speaking_service, monkeypatch):
    """tts_service refuses anything over its limit outright, which for a
    request to read back a long explanation would be silence and an
    error where a person expects to hear the beginning."""
    from app.voice.tts import MAX_SPEAK_LENGTH

    monkeypatch.setattr(speak_reply, "last_assistant_reply", lambda: "x" * (MAX_SPEAK_LENGTH + 500))

    result = speak_reply._speak_last_reply()

    assert result["success"] is True
    assert result["data"]["truncated"] is True
    spoken = speaking_service.speak.call_args.args[0]
    assert len(spoken) == MAX_SPEAK_LENGTH


def test_it_reads_the_most_recent_assistant_turn(monkeypatch):
    from app.core.ai.base import Message
    from app.core import conversation

    monkeypatch.setattr(conversation, "recent_messages", lambda *_a, **_k: [
        Message(role="user", content="first question"),
        Message(role="assistant", content="first answer"),
        Message(role="user", content="second question"),
        Message(role="assistant", content="second answer"),
    ])

    assert speak_reply.last_assistant_reply() == "second answer"


# ---------------------------------------------------------------------------
# When it cannot speak: the cause and the step, never another program
# ---------------------------------------------------------------------------

def test_an_unavailable_engine_reports_the_engines_own_reason(monkeypatch):
    service = MagicMock()
    service.is_available.return_value = False
    service.voice_key = "bm_george"

    with patch("app.voice.tts.tts_service", service), \
         patch("app.voice.engines.unavailable_message", return_value="The neural voice is not installed yet. Install it from the Voice page."):
        result = speak_reply._speak_last_reply()

    assert result["success"] is False
    assert "Install it from the Voice page" in result["message"]
    assert result["data"]["reason"] == "engine_unavailable"


@pytest.mark.parametrize("forbidden", ["Narrator", "NaturalReader", "Google Docs", "extension"])
def test_no_failure_message_sends_the_user_to_another_program(forbidden):
    """The whole defect in one assertion. Checked against the code and
    its user-visible strings, not its prose — the docstring names these
    three deliberately, because they are what JARVIS actually offered."""
    assert forbidden not in _source_without_prose()


def test_no_previous_reply_says_so_plainly(speaking_service, monkeypatch):
    monkeypatch.setattr(speak_reply, "last_assistant_reply", lambda: "")
    monkeypatch.setattr("app.core.conversation.history_enabled", lambda: True)

    result = speak_reply._speak_last_reply()

    assert result["success"] is False
    assert result["data"]["reason"] == "no_previous_reply"
    assert "haven't said anything yet" in result["message"]


def test_privacy_mode_is_named_as_the_reason_rather_than_looking_like_amnesia(speaking_service, monkeypatch):
    """While privacy mode is on nothing is stored, so nothing is found.
    Reporting that as "I haven't said anything" would be false and would
    look like a bug in the feature that is working correctly."""
    monkeypatch.setattr(speak_reply, "last_assistant_reply", lambda: "")
    monkeypatch.setattr("app.core.conversation.history_enabled", lambda: False)

    result = speak_reply._speak_last_reply()

    assert result["success"] is False
    assert result["data"]["reason"] == "privacy_mode"
    assert "Privacy mode is on" in result["message"]


def test_a_failed_utterance_reports_the_engines_message(speaking_service, monkeypatch):
    monkeypatch.setattr(speak_reply, "last_assistant_reply", lambda: "something")
    speaking_service.speak.return_value = MagicMock(success=False, message="The audio device is in use.")

    result = speak_reply._speak_last_reply()

    assert result["success"] is False
    assert result["message"] == "The audio device is in use."


# ---------------------------------------------------------------------------
# The always-speak switch is offered, never rewritten
# ---------------------------------------------------------------------------

def test_an_explicit_request_is_honoured_while_always_speak_is_off(speaking_service, monkeypatch):
    """`output_enabled` answers "speak every reply automatically".
    Someone who has just typed "read that aloud" has asked for this one,
    and refusing it because a different setting is off would be the same
    class of bug as the one being fixed. This is how tts_test has always
    behaved, so there is still exactly one flag."""
    speaking_service.output_enabled = False
    monkeypatch.setattr(speak_reply, "last_assistant_reply", lambda: "the answer")

    result = speak_reply._speak_last_reply()

    assert result["success"] is True
    speaking_service.speak.assert_called_once()
    assert "speak on" in result["message"], "the way to turn it on must be offered"


def test_a_chat_message_never_rewrites_the_saved_setting(speaking_service, monkeypatch):
    speaking_service.output_enabled = False
    monkeypatch.setattr(speak_reply, "last_assistant_reply", lambda: "the answer")

    speak_reply._speak_last_reply()

    speaking_service.set_output_enabled.assert_not_called()


def test_with_always_speak_on_it_does_not_nag(speaking_service, monkeypatch):
    monkeypatch.setattr(speak_reply, "last_assistant_reply", lambda: "the answer")

    result = speak_reply._speak_last_reply()

    assert "speak on" not in result["message"]


# ---------------------------------------------------------------------------
# No overlapping speech in the CLI
# ---------------------------------------------------------------------------

def test_the_cli_does_not_speak_over_the_answer_it_just_started():
    """The tool has already started speaking the previous answer. Speaking
    its own confirmation on top of that is two voices at once."""
    from app.main import _TTS_CONTROL_TOOLS

    assert "speak_last_reply" in _TTS_CONTROL_TOOLS


# ---------------------------------------------------------------------------
# Output only — CLAUDE.md's Phase 3 rule
# ---------------------------------------------------------------------------

def test_nothing_here_opens_a_microphone():
    source = _source_without_prose().lower()

    for forbidden in ("microphone", "sounddevice", "pyaudio", "wake", "listen"):
        assert forbidden not in source
