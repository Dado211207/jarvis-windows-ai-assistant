"""PyInstaller entry point for the JARVIS Windows executable.

This script is the sole entry point used by PyInstaller (`pyinstaller run_jarvis.py`).
It delegates entirely to the existing CLI in app.main — no business logic lives here.
"""

from app.main import main

if __name__ == "__main__":
    main()
