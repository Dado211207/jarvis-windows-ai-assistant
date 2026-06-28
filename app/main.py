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
