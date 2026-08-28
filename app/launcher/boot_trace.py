"""Boot-time diagnostic trace — deliberately independent of
app/logging_config.py's logging setup.

Exists specifically to stay useful if the logging subsystem itself is
what's failing to explain a startup problem: a real frozen JARVIS.exe on
windows-latest CI launched, stayed running (no crash, no early exit),
never answered /health, and left its own jarvis.log completely empty
even after 30+ seconds — meaning whatever went wrong happened before any
logger.*() call anywhere in the startup path ever ran, or the logging
setup itself was silently swallowing every record. Plain file I/O here
has no such dependency to fail alongside.

Append-only, best-effort, and never raises — a broken trace call must
never be the thing that breaks startup; see app/core/credentials.py and
app/launcher/server_runner.py's own "external call can never crash the
caller" precedent for the same reasoning applied elsewhere in this
package.
"""

import time


def trace(message: str) -> None:
    """A genuine no-op outside a packaged/frozen build. Dev and test
    runs already have working console output and pytest's own capture —
    they don't need this, and app_data_root() resolves to the
    repository root in dev mode, which would otherwise leak a
    boot_trace.log file into the working tree on every test run (the
    same class of problem already fixed once for jarvis.lock — see
    .gitignore)."""
    try:
        from app.core.app_paths import app_data_root, is_frozen

        if not is_frozen():
            return
        path = app_data_root() / "boot_trace.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass
