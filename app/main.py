"""JARVIS CLI — run with: python -m app.main"""

import sys

from app.logging_config import setup_logging
from app import __version__, __phase__

BANNER = f"""
╔══════════════════════════════════════════════════════╗
║  JARVIS — Personal Windows AI Assistant              ║
║  {__version__}  |  {__phase__:<38}║
╚══════════════════════════════════════════════════════╝
Type 'help' to list commands, 'exit' to quit.
"""

PROMPT = "jarvis> "

# TTS control tools manage their own speech — don't auto-speak their
# responses. speak_last_reply is here for the same reason and a sharper
# one: it has just started speaking the previous answer, and speaking its
# own confirmation on top of that would overlap two utterances.
_TTS_CONTROL_TOOLS = {
    "tts_enable", "tts_disable", "tts_status", "tts_test", "tts_stop", "speak_last_reply",
}


def main() -> None:
    setup_logging()

    from app.core.brain import brain
    brain.initialise()

    print(BANNER)

    while True:
        try:
            raw = input(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            sys.exit(0)

        if not raw:
            continue

        response = brain.process(raw)

        if response.success:
            print(response.message)
        else:
            print(f"[!] {response.message}")

        if response.data and isinstance(response.data, dict) and response.data.get("exit"):
            sys.exit(0)

        if response.data:
            _maybe_print_data(response.data)

        # Auto-speak successful responses when spoken replies are on.
        # TTS control commands handle their own output — skip them here.
        if response.success and response.tool_used not in _TTS_CONTROL_TOOLS:
            from app.voice.tts import tts_service
            if tts_service.output_enabled:
                tts_service.speak(response.message)

        print()


def _maybe_print_data(data) -> None:
    """Pretty-print structured data when it adds value beyond the message."""
    if isinstance(data, list) and data:
        for item in data:
            if isinstance(item, dict):
                for k, v in item.items():
                    print(f"    {k}: {v}")
                print()
    elif isinstance(data, dict):
        skip_keys = {"exit"}
        for k, v in data.items():
            if k not in skip_keys and v is not None:
                print(f"    {k}: {v}")


if __name__ == "__main__":
    main()
