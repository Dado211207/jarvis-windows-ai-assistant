"""`JARVIS.exe --uninstall-cleanup` — the half of uninstalling that the
installer cannot do.

Inno Setup removes the files it installed. It has never heard of the
sign-in shortcut the *application* writes when somebody switches that on
in Settings, and it does not know how saved credentials were stored,
because
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

**It prints what it did, item by item**, writes the same result to the
installer-supplied report path, and returns a non-zero status when
cleanup is incomplete. The installer uses both signals to keep the data
folder (including that report) instead of erasing the only evidence a
person has for manual recovery.
"""

import json
from pathlib import Path
from typing import List

from app.launcher.safe_output import flush, say

MARKER = "UNINSTALL_JSON "
REPORT_ARG = "--report-file="
EXIT_OK = 0
EXIT_CLEANUP_INCOMPLETE = 2
EXIT_REPORT_UNAVAILABLE = 3


def _report_path(argv: List[str]) -> Path | None:
    for arg in argv:
        if arg.startswith(REPORT_ARG):
            value = arg[len(REPORT_ARG):].strip()
            return Path(value) if value else None
    return None


def _write_report(path: Path, payload: dict) -> bool:
    """Atomically replace *path*, leaving the previous report on failure."""
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return True
    except BaseException:  # noqa: BLE001 — this runs inside the uninstaller
        try:
            temporary.unlink(missing_ok=True)
        except BaseException:  # noqa: BLE001
            pass
        return False


def run(argv: List[str]) -> int:
    """Remove what the application created and report whether it worked.

    **Nothing in here may raise, and nothing may block.** The uninstaller
    launches this with `Exec(..., SW_HIDE, ewWaitUntilTerminated)` and
    waits for it, in a build with no console: an unhandled exception
    becomes a modal dialog nobody can see, and the uninstall hangs behind
    it until something kills the process. That is exactly what happened
    the first time this ran on a real Windows machine — the progress
    output below, harmless everywhere that supplies a pipe, wrote to a
    `None` stdout and stopped a silent uninstall dead for two minutes.

    Hence safe_output for every line, a catch-all around the work, and a
    durable report outside the data directory. A non-zero exit does not
    stop Inno Setup; it tells it to keep the data directory and surface
    an honest manual-recovery message.
    """
    purge = "--purge-data" in argv
    report_path = _report_path(argv)

    # Prove that the durable report destination is writable *before* a
    # destructive purge. If this fails, leave every other piece of
    # evidence in place and do not start cleanup.
    if report_path is not None and not _write_report(
        report_path,
        {"status": "starting", "purge_data": purge},
    ):
        say("JARVIS uninstall cleanup did not run because its report could not be created.")
        flush()
        return EXIT_REPORT_UNAVAILABLE

    try:
        from app.core.ownership import remove

        report = remove(purge_data=purge)
    except BaseException as exc:  # noqa: BLE001 — must never reach the bootloader
        payload = {
            "status": "incomplete",
            "purge_data": purge,
            "removed": [],
            "not_present": [],
            "failed": [f"Cleanup stopped unexpectedly ({type(exc).__name__})."],
            "kept": ["The JARVIS data folder for recovery"],
        }
        say("JARVIS uninstall cleanup could not run.")
        say(MARKER + json.dumps(payload))
        if report_path is not None:
            _write_report(report_path, payload)
        flush()
        return EXIT_CLEANUP_INCOMPLETE

    say("JARVIS uninstall cleanup")
    for line in report.removed:
        say(f"  removed:     {line}")
    for line in report.not_present:
        say(f"  not present: {line}")
    for line in report.failed:
        say(f"  FAILED:      {line}")
    for line in report.kept:
        say(f"  kept:        {line}")

    status = "incomplete" if report.failed else "complete"
    payload = dict(report.as_dict(), status=status, purge_data=purge)
    say(MARKER + json.dumps(payload))

    if report_path is not None and not _write_report(report_path, payload):
        say("JARVIS uninstall cleanup finished, but its final report could not be saved.")
        flush()
        return EXIT_REPORT_UNAVAILABLE

    flush()
    return EXIT_CLEANUP_INCOMPLETE if report.failed else EXIT_OK
