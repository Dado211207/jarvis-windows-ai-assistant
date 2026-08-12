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
import sys
from typing import List

MARKER = "UNINSTALL_JSON "


def run(argv: List[str]) -> int:
    from app.core.ownership import remove

    purge = "--purge-data" in argv

    report = remove(purge_data=purge)

    print("JARVIS uninstall cleanup")
    for line in report.removed:
        print(f"  removed:     {line}")
    for line in report.not_present:
        print(f"  not present: {line}")
    for line in report.failed:
        print(f"  FAILED:      {line}")
    for line in report.kept:
        print(f"  kept:        {line}")

    payload = dict(report.as_dict(), purge_data=purge)
    print(MARKER + json.dumps(payload))
    sys.stdout.flush()
    return 0
