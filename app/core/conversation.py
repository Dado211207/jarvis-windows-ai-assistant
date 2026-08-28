"""Conversation history: what the AI is told, what gets stored, and how
a user throws it away.

This exists because three call sites need to agree exactly — the
non-streaming /command path, the streaming /chat/stream path, and the
reset endpoint. When "does privacy mode suppress this?" was answered
independently in each of them, one of them was eventually going to
answer it differently.

**History is sent to the provider.** A chat where every message is the
first message is not a chat; "what about tomorrow?" has to mean
something. So the last few turns are replayed on each request, bounded
by MAX_HISTORY_TURNS — bounded because an unbounded transcript grows the
cost and latency of every message and eventually exceeds the model's
context anyway.

**Privacy mode suppresses history entirely.** app/core/privacy.py already
guarantees new turns are not written while it is on, and that stored
memory is excluded from provider requests. Reading stored turns back out
and sending them to a cloud provider would drive a hole straight through
the second of those guarantees, so while privacy mode is active a
request carries only the message just typed.

**Nothing here is encryption.** The conversations table is rows in an
unencrypted SQLite file; reset deletes them. See docs/THREAT_MODEL.md.
"""

from typing import List

from app.core.ai.base import Message
from app.logging_config import get_logger

logger = get_logger("core.conversation")

# Six turns ≈ three exchanges: enough for a follow-up question to make
# sense, small enough that it never quietly becomes the dominant cost of
# a short message.
MAX_HISTORY_TURNS = 6

_VALID_ROLES = ("user", "assistant")


def history_enabled() -> bool:
    """False while privacy mode is on — see the module docstring."""
    try:
        from app.core.privacy import privacy_mode

        return not privacy_mode.active
    except Exception:  # noqa: BLE001
        # If the privacy module can't be consulted, assume the stricter
        # answer. Failing closed here costs a little context; failing
        # open would send stored turns somewhere the user said not to.
        return False


def recent_messages(limit: int = MAX_HISTORY_TURNS) -> List[Message]:
    """Prior turns in chronological order, oldest first, ready to send.

    Never raises: a broken or missing database must degrade the chat to
    "no memory of earlier turns", not break it.
    """
    if not history_enabled():
        return []

    try:
        from db.database import get_db

        rows = get_db().get_recent_conversations(limit=limit)
        entries = list(rows)
    except Exception:  # noqa: BLE001
        logger.debug("Conversation history unavailable; continuing without it.", exc_info=True)
        return []

    messages: List[Message] = []
    for entry in reversed(entries):  # DB returns newest-first
        role = getattr(entry, "role", None)
        content = getattr(entry, "content", None)
        if role in _VALID_ROLES and isinstance(content, str) and content:
            messages.append(Message(role=role, content=content))
    return messages


def build_request_messages(command: str) -> List[Message]:
    """History plus the message just typed."""
    return recent_messages() + [Message(role="user", content=command)]


def record_exchange(user_text: str, assistant_text: str) -> bool:
    """Persist one exchange unless privacy mode is on. Returns whether it
    was written. Never raises — a failed write must not turn a successful
    answer into an error."""
    if not history_enabled():
        return False
    try:
        from db.database import get_db

        db = get_db()
        db.add_conversation("user", user_text)
        db.add_conversation("assistant", assistant_text)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("Could not persist conversation turn.", exc_info=True)
        return False


def reset() -> int:
    """Delete every stored conversation turn. Returns the number removed.

    Irreversible, and the UI says so before calling this. It is scoped to
    the conversations table only: the action_logs audit trail and the
    action_lifecycle records are deliberately untouched, because "clear
    my chat" must not double as a way to erase what JARVIS was asked to
    do on this machine.
    """
    from db.database import get_db

    removed = get_db().clear_conversations()
    logger.info("Conversation history cleared: %d turn(s) removed.", removed)
    return removed
