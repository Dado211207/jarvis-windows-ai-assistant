"""`JARVIS.exe --uninstall-cleanup` — the half of uninstalling that the
installer cannot do.

Inno Setup removes the files it installed. It has never heard of the
sign-in shortcut the *application* writes when somebody switches that on
in Settings, and it does not know how the API key was stored, because
only `app/core/credentials.py` knows that. An installer guessing at a
Windows Credential Manager target name is how an uninstall leaves a
secret behind while reporting success.

So the uninstaller calls the application, once, before the files go —
`packaging/jarvis.iss`, `usUninstall`. Two flags and nothing else:

    JARVIS.exe --uninstall-cleanup                 remove what we created
    JARVIS.exe --uninstall-cleanup --purge-data    …and the data as well

**Never destructive by default.** Without `--purge-data`, settings, chat
history and downloaded models stay, which is what somebody reinstalling
next week wants. The flag is never inferred.

**It prints what it did, item by item**, and exits 0 even when something
could not be removed. An uninstaller cannot usefully fail — the files
are going regardless — so the useful behaviour is to report accurately
rather than to abort halfway and leave a half-removed installation.
"""

import json
from typing import List

from app.launcher.safe_output import flush, say

MARKER = "UNINSTALL_JSON "


def run(argv: List[str]) -> int:
    """Remove what the application created, then exit. Always 0.

    **Nothing in here may raise, and nothing may block.** The uninstaller
    launches this with `Exec(..., SW_HIDE, ewWaitUntilTerminated)` and
    waits for it, in a build with no console: an unhandled exception
    becomes a modal dialog nobody can see, and the uninstall hangs behind
    it until something kills the process. That is exactly what happened
    the first time this ran on a real Windows machine — the progress
    output below, harmless everywhere that supplies a pipe, wrote to a
    `None` stdout and stopped a silent uninstall dead for two minutes.

    Hence safe_output for every line, and a catch-all around the work:
    an uninstall cleanup that fails should say so and let the uninstall
    continue, because the files are going regardless and a half-removed
    installation is worse than an orphaned shortcut.
    """
    purge = "--purge-data" in argv

    try:
        from app.core.ownership import remove

        report = remove(purge_data=purge)
    except BaseException as exc:  # noqa: BLE001 — must never reach the bootloader
        say(f"JARVIS uninstall cleanup could not run: {exc!r}")
        flush()
        return 0

    say("JARVIS uninstall cleanup")
    for line in report.removed:
        say(f"  removed:     {line}")
    for line in report.not_present:
        say(f"  not present: {line}")
    for line in report.failed:
        say(f"  FAILED:      {line}")
    for line in report.kept:
        say(f"  kept:        {line}")

    payload = dict(report.as_dict(), purge_data=purge)
    say(MARKER + json.dumps(payload))
    flush()
    return 0
