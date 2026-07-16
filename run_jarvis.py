"""PyInstaller entry point for the JARVIS Windows executable.

Delegates to the CLI by default. Pass --api to start the local FastAPI server
directly, or --cli to force the terminal CLI. In a frozen build with neither
flag, delegates to the no-console production launcher instead of the CLI
(which needs a real terminal to read stdin). No business logic lives here —
this file is the sole PyInstaller entry point and must stay minimal.
"""

import sys


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def _run() -> None:
    if "--api" in sys.argv[1:]:
        from app.api.server import run_api
        run_api()
    elif "--cli" in sys.argv[1:]:
        from app.main import main
        main()
    elif _is_frozen():
        from app.core.launcher import run_production
        sys.exit(run_production())
    else:
        from app.main import main
        main()


if __name__ == "__main__":
    _run()
