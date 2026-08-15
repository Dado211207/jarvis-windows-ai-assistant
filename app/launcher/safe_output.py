"""Printing that cannot hang the installed application.

The packaged JARVIS.exe is built `--windowed` (console=False). Such a
build has no console, and depending on how it was started `sys.stdout`
may be `None` — in which case a bare `print()` raises AttributeError.

That is not a harmless traceback. PyInstaller's windowed bootloader
turns an unhandled exception into a modal error dialog that nobody is
there to dismiss, so the process never exits. Anything waiting on it
waits forever.

This has now cost this project twice. First a `--windowed` build hung
because uvicorn's `configure_logging()` wrote to a `None` stream. Then
`JARVIS.exe --uninstall-cleanup`, launched by the uninstaller with
`Exec(..., SW_HIDE, ewWaitUntilTerminated)`, hung on its own progress
output — leaving a real uninstall stuck behind an invisible dialog:

    === Phase B.1: Silent uninstall (no /DELETEDATA flag) ===
    FAILED: unins000.exe did not finish within 120.0s
    ...
    Terminate orphan process: pid (8760) (JARVIS)

The same code prints perfectly well when something gives it a pipe,
which is why it passes every test that captures output and fails only
where it matters.
"""

import sys


def say(message: str = "") -> None:
    """Print *message* if there is anywhere to print it. Never raises.

    A subcommand's console output is a convenience for whoever is
    watching; it is never worth the installed application hanging over.
    """
    try:
        stream = sys.stdout
        if stream is None:
            return
        stream.write(message + "\n")
    except Exception:  # noqa: BLE001 — the whole point is that output cannot fail
        return


def flush() -> None:
    """Flush stdout if it exists. Never raises."""
    try:
        if sys.stdout is not None:
            sys.stdout.flush()
    except Exception:  # noqa: BLE001
        return
