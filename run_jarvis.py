"""PyInstaller entry point for the JARVIS Windows executable.

Default (no arguments): the real packaged-app experience — single
instance guard, background server, health-wait, a native desktop window
(falling back to the default browser if the native window can't be
created — see app/launcher/webview_window.py), and a system tray control
(app/launcher/). This is also the only correct default for the installed
JARVIS.exe, which is built --windowed (console=False): that build has no
console at all, so the interactive CLI below cannot function in it
regardless of arguments.

--api starts only the local FastAPI server (headless, console-mode
builds/dev use), unchanged from before this file grew a windowed mode.

--cli runs the original interactive REPL (app.main) — preserved,
not removed, for anyone building/running a console-mode executable
who explicitly wants it; no longer the default now that the packaged
build has no console for it to write to.

No business logic lives here — this file is the sole PyInstaller entry
point and must stay minimal.
"""

import sys


def _run() -> None:
    argv = sys.argv[1:]
    if "--api" in argv:
        from app.api.server import run_api
        run_api()
    elif "--cli" in argv:
        from app.main import main
        main()
    else:
        from app.launcher.gui import run_windowed
        run_windowed()


if __name__ == "__main__":
    _run()
