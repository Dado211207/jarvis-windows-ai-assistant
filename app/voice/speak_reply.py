""""Say that out loud" — the request JARVIS used to answer with a link
to somebody else's software.

From a real session on the installed release candidate:

    User:   answer me with your voice
    JARVIS: I appreciate the request, Dado, but I don't have
            text-to-speech capabilities built into this interface right
            now. I can only communicate through text.

It then suggested Windows Narrator, NaturalReader and Google Docs.

app/core/capabilities.py stops the model *believing* that. This module
stops it *mattering*: "answer me with your voice", "say that again",
"read this aloud" and their neighbours are deterministic routes now, so
they reach the speech engine directly and never depend on a model's
opinion of itself at all. CLAUDE.md's Phase 2 rule already says
deterministic routes win; this is one more thing that should never have
been a guess.

**What gets spoken is the previous answer**, read back from conversation
history. Not a fresh generation: "say that" means *that*, and asking a
model to produce a second, differently-worded answer to speak would make
the spoken version and the written one disagree.

**An explicit request is not gated on the always-speak switch.**
`output_enabled` answers "speak every reply automatically". Somebody who
has just typed "read that aloud" has asked for this one, and refusing it
because a different setting is off would be the same class of bug as the
one being fixed. This is exactly how `tts_test` has always behaved, so
it introduces no second flag — CLAUDE.md's Phase 3 rule is about there
being one switch, and there still is one. What is off is *offered*, not
silently changed: a chat message must not rewrite a saved setting.

**Nothing here opens a microphone.** Output only, as ever.
"""

from app.logging_config import get_logger

logger = get_logger("voice.speak_reply")

# Long answers are read from the start rather than refused. tts_service
# rejects anything over MAX_SPEAK_LENGTH outright, which for a request to
# read back a long explanation would mean silence and an error where a
# person expects to hear the beginning of it.
_SPOKEN_PREFIX_HINT = "…that's the first part — the rest is on screen."


def last_assistant_reply() -> str:
    """The most recent thing JARVIS said, or "".

    Read from app/core/conversation.py rather than a buffer of its own,
    so privacy mode's guarantee holds without a second implementation of
    it: while privacy mode is on nothing is stored, so nothing is found,
    and _speak_last_reply() says so rather than pretending it forgot.
    """
    from app.core.conversation import recent_messages

    for message in reversed(recent_messages()):
        if message.role == "assistant" and message.content.strip():
            return message.content.strip()
    return ""


def _nothing_to_say() -> dict:
    """No previous answer. Which of the two reasons it is matters."""
    from app.core.conversation import history_enabled

    if not history_enabled():
        return {
            "success": False,
            "message": (
                "Privacy mode is on, so I'm not keeping a record of what I said — "
                "there's no previous answer for me to read back. Ask me the question "
                "again and I'll speak the answer, or turn privacy mode off with "
                "'privacy off'."
            ),
            "data": {"spoken": False, "reason": "privacy_mode"},
        }
    return {
        "success": False,
        "message": "I haven't said anything yet in this conversation for me to read back.",
        "data": {"spoken": False, "reason": "no_previous_reply"},
    }


def _speak_last_reply() -> dict:
    """Read the previous answer out loud, or say exactly why not."""
    from app.voice import engines
    from app.voice.tts import MAX_SPEAK_LENGTH, tts_service

    if not tts_service.is_available():
        # The cause and the step that fixes it, from the engine that
        # knows — never "I can't speak", and never somebody else's app.
        return {
            "success": False,
            "message": engines.unavailable_message(tts_service.voice_key),
            "data": {"spoken": False, "reason": "engine_unavailable"},
        }

    text = last_assistant_reply()
    if not text:
        return _nothing_to_say()

    truncated = len(text) > MAX_SPEAK_LENGTH
    spoken_text = text[:MAX_SPEAK_LENGTH] if truncated else text

    result = tts_service.speak(spoken_text)
    if not result.success:
        return {
            "success": False,
            "message": result.message,
            "data": {"spoken": False, "reason": "speech_failed"},
        }

    message = "Reading my last answer aloud."
    if truncated:
        message = f"Reading my last answer aloud {_SPOKEN_PREFIX_HINT}"
    if not tts_service.output_enabled:
        message += (
            " Spoken replies are switched off, so this was a one-off — say "
            "'speak on' and I'll speak every reply."
        )
    return {
        "success": True,
        "message": message,
        "data": {
            "spoken": True,
            "engine": tts_service.active_engine(),
            "always_speak": tts_service.output_enabled,
            "truncated": truncated,
        },
    }


def register_tools(registry) -> None:
    from app.core.models import PermissionLevel, ToolCategory, ToolDefinition

    registry.register(
        ToolDefinition(
            name="speak_last_reply",
            description=(
                "Read JARVIS's previous answer out loud using its own built-in voice "
                "('say that', 'read this aloud', 'answer me with your voice')."
            ),
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.VOICE,
        ),
        _speak_last_reply,
    )
