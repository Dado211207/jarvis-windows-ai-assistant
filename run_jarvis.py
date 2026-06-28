"""PyInstaller entry point for the JARVIS Windows executable.

Delegates to the CLI by default. Pass --api to start the local FastAPI server
instead. No business logic lives here — this file is the sole PyInstaller entry
point and must stay minimal.
"""

import sys


def _run() -> None:
    if "--api" in sys.argv[1:]:
        from app.api.server import run_api
        run_api()
    else:
        from app.main import main
        main()


if __name__ == "__main__":
    _run()
