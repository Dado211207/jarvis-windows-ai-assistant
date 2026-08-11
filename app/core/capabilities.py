"""What JARVIS can actually do right now, in words the model can use.

The defect this exists for, verbatim from a real session:

    User:   answer me with your voice
    JARVIS: I appreciate the request, Dado, but I don't have
            text-to-speech capabilities built into this interface right
            now. I can only communicate through text.

It then recommended Windows Narrator, NaturalReader and Google Docs.

Nothing was broken except the prompt. JARVIS has a complete TTS stack —
a neural voice, a Windows fallback and a classic tier — and the model
was never told. Asked a question about itself, it answered from general
knowledge about chat interfaces, which is exactly what a model does when
it has no information: it guesses, plausibly, and wrongly.

**Computed per request, never cached.** A speech engine can become
available (a voice finishes downloading) or unavailable (a model file is
deleted) while the app runs, and a remembered answer would be wrong in
precisely the moment the user is asking about it.

**Facts, not instructions.** Everything here is generated from internal
state — booleans, an enum-like engine key, a voice name from a fixed
set. No user-supplied string is interpolated, so this block cannot carry
an injected instruction into the system prompt. The one piece of user
text that does reach the prompt, the preferred name, is sanitised by
system_prompt.py and stays that module's responsibility.
"""

from dataclasses import dataclass
from typing import List

from app.logging_config import get_logger

logger = get_logger("core.capabilities")


@dataclass(frozen=True)
class Capabilities:
    """A snapshot. Every field is a fact the model may state as true."""

    speech_output_ready: bool
    speech_output_engine: str
    speech_output_detail: str
    speaks_replies: bool
    voice_name: str

    speech_input_ready: bool
    speech_input_detail: str

    local_ai_ready: bool
    local_ai_detail: str

    desktop_actions: bool


def _safe(call, fallback):
    """Capability detection must never break a chat request.

    A user asking a question is not the moment to raise because a voice
    file is unreadable. An unknown capability is reported as absent,
    which is the honest reading and the one that produces a cautious
    answer rather than a false claim.
    """
    try:
        return call()
    except Exception:  # noqa: BLE001
        logger.warning("Capability detection failed; reporting it as unavailable.", exc_info=True)
        return fallback


def snapshot() -> Capabilities:
    from app.voice import engines
    from app.voice.kokoro import assets
    from app.voice.tts import tts_service

    voice_key = _safe(engines.selected_voice_key, assets.DEFAULT_VOICE_KEY)
    statuses = _safe(lambda: engines.statuses(voice_key), [])
    active = next((status for status in statuses if status.active), None)

    def _voice_name() -> str:
        return assets.resolve_voice(voice_key).display_name

    def _stt():
        from app.voice.stt import stt_service

        return stt_service.runtime_status()

    def _local_ai():
        from app.core import local_ai

        state = local_ai.describe()
        return state.usable, f"{state.headline} {state.next_step}".strip()

    stt_ready, stt_detail = _safe(_stt, (False, "Speech recognition could not be checked."))
    local_ready, local_detail = _safe(_local_ai, (False, "Local AI could not be checked."))

    # When nothing can speak, the engine's own tier detail says only that
    # *that* tier is unusable. unavailable_message() is the one that names
    # the step which actually fixes it, and naming a step is the whole
    # point: the reported failure was JARVIS sending someone to go and
    # find a different program.
    if active is not None:
        detail = active.detail
    else:
        detail = _safe(
            lambda: engines.unavailable_message(voice_key),
            "No speech engine is available on this machine.",
        )

    return Capabilities(
        speech_output_ready=active is not None,
        speech_output_engine=active.display_name if active else engines.DISPLAY_NAMES[engines.NONE],
        speech_output_detail=str(detail),
        speaks_replies=_safe(lambda: tts_service.output_enabled, False),
        voice_name=_safe(_voice_name, ""),
        speech_input_ready=bool(stt_ready),
        speech_input_detail=str(stt_detail),
        local_ai_ready=bool(local_ready),
        local_ai_detail=str(local_detail),
        desktop_actions=True,
    )


def describe_for_prompt(state: Capabilities) -> str:
    """The snapshot as prompt text.

    Written as plain statements about this application rather than as
    instructions, because the model's job here is to stop guessing about
    itself — and because a block of imperatives appended to a prompt is
    an obvious place for a rule to be accidentally weakened.
    """
    lines: List[str] = ["Your current capabilities on this machine:"]

    if state.speech_output_ready:
        voice = f" with the {state.voice_name} voice" if state.voice_name else ""
        lines.append(
            f"- Text-to-speech: WORKING. You have your own built-in voice, running "
            f"locally through {state.speech_output_engine}{voice}. You can speak out "
            "loud. Asked to speak, to read something aloud, or to answer with your "
            "voice, say that you will and let the JARVIS speech system say it — never "
            "answer that you cannot speak."
        )
        lines.append(
            "- Speaking every reply aloud is currently "
            + ("ON" if state.speaks_replies else "OFF")
            + ". "
            + (
                "You may be asked to turn it off."
                if state.speaks_replies
                else "That switch only controls whether every reply is spoken "
                "automatically; you can still speak a single answer on request, and "
                "you can offer to turn it on."
            )
        )
    else:
        lines.append(
            "- Text-to-speech: NOT USABLE RIGHT NOW. You do have a built-in voice; it "
            f"cannot run at this moment. The exact reason is: {state.speech_output_detail} "
            "Give that reason and that step. Do not send the user to another program."
        )

    lines.append(
        f"- Voice input (push-to-talk): {'AVAILABLE' if state.speech_input_ready else 'NOT AVAILABLE'}. "
        f"{state.speech_input_detail}"
    )
    lines.append(
        f"- Local AI (Ollama, offline): {'READY' if state.local_ai_ready else 'NOT READY'}. "
        f"{state.local_ai_detail}"
    )
    if state.desktop_actions:
        lines.append(
            "- Safe Windows actions: AVAILABLE through the JARVIS tool system — opening "
            "allowlisted apps, folders and websites, writing notes, reporting system "
            "information, locking the screen."
        )
    return "\n".join(lines)


def describe_now() -> str:
    """Convenience for the one caller that wants both steps."""
    return describe_for_prompt(snapshot())
