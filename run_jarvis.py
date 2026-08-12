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
        # Internal mode: the server child. Started by the parent launcher
        # (app/launcher/server_process.py), never by a Start-menu
        # shortcut.
        from app.api.server import run_api
        run_api()
    elif "--window" in argv:
        # Internal mode: the window child. Started by the parent launcher
        # (app/launcher/window_process.py) with an inherited IPC context;
        # exits with a clear message if that context is absent, so running
        # it by hand is harmless rather than confusing.
        from app.launcher.window_main import main as window_main
        sys.exit(window_main(argv))
    elif "--selftest" in argv:
        # Asks the *installed* executable what it can actually do. The
        # release candidate shipped with no speech input while every
        # source-tree check passed, because a source-tree import proves
        # only that the build machine has the package. See
        # app/launcher/selftest.py.
        from app.launcher.selftest import run as selftest_run
        sys.exit(selftest_run(argv))
    elif "--uninstall-cleanup" in argv:
        # Called by the uninstaller, before the files go. Removes what
        # the *application* created and the installer has never heard of
        # — the sign-in shortcut, and (only with --purge-data) the stored
        # API key and the data folder. See app/launcher/uninstall.py.
        from app.launcher.uninstall import run as uninstall_run
        sys.exit(uninstall_run(argv))
    elif "--cli" in argv:
        from app.main import main
        main()
    else:
        from app.launcher.gui import run_windowed
        run_windowed()


if __name__ == "__main__":
    _run()
