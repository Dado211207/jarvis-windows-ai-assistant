"""Detects the runtimes the native window depends on, before trying to
use them.

The packaged app has two hard dependencies it cannot satisfy itself: the
Microsoft **WebView2 Runtime** (the Chromium control the window renders
in) and a **.NET runtime** (pywebview's only Windows backend is WinForms
via pythonnet). When either is absent, `webview.start()` fails deep
inside a third-party library with a message no user can act on.

Before this module existed, the launcher's response to that failure was
to silently open the user's default browser. That is the wrong answer
twice over: it hides a fixable problem, and it makes the *browser* the
product's real interface — which the owner explicitly rejected. So the
check happens up front, the failure names which runtime is missing, and
the user is offered the download page.

Everything here is Windows-specific and read-only: a registry lookup and
an import probe. Nothing is installed, downloaded or modified. Off
Windows every check returns "not applicable" rather than pretending, so
this module stays importable on the Linux CI that runs the tests.
"""

import sys
from dataclasses import dataclass
from typing import Optional

from app.logging_config import get_logger

logger = get_logger("launcher.runtime_check")

# Microsoft's own documented detection key for the Evergreen WebView2
# Runtime. Both the per-machine (HKLM, WOW6432Node on 64-bit) and
# per-user (HKCU) locations are checked, because a per-user install is
# invisible in the machine hive and vice versa.
WEBVIEW2_CLIENT_KEY = r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"

# The user-facing text lives in constants, not inside the probes, because
# describe() explains a failure that has *already been reported by the
# window child* — re-probing the local machine to write that sentence
# would let the dialog contradict the report (a runtime installed in the
# seconds between the failure and the dialog would otherwise produce
# "Installed (version …)" as the explanation for why nothing opened).
WEBVIEW2_MISSING_MESSAGE = (
    "The Microsoft WebView2 Runtime is not installed, so JARVIS cannot "
    "draw its window. It is a free Microsoft component and installing it "
    "takes about a minute."
)
DOTNET_MISSING_MESSAGE = (
    "The .NET runtime JARVIS's window needs could not be loaded. "
    "Repairing or reinstalling JARVIS usually fixes this."
)
WINDOW_FAILED_MESSAGE = (
    "JARVIS could not open its window. The details are in the log folder "
    "below."
)


@dataclass
class RuntimeStatus:
    """What was actually checked, and what to tell the user.

    `available` is only ever True when something real was verified —
    never inferred from the platform alone.
    """

    name: str
    available: bool
    detail: str
    fix_url: Optional[str] = None
    applicable: bool = True


def _read_webview2_version() -> Optional[str]:
    """The installed WebView2 Runtime version string, or None.

    Returns None (never raises) on any registry error: a failed lookup
    means "could not confirm it is installed", which is the same
    actionable state as absent, and a crash here would take down the
    launcher over a diagnostic.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows only
        return None

    candidates = (
        (winreg.HKEY_LOCAL_MACHINE, rf"Software\WOW6432Node\{WEBVIEW2_CLIENT_KEY[len('Software')+1:]}"),
        (winreg.HKEY_LOCAL_MACHINE, WEBVIEW2_CLIENT_KEY),
        (winreg.HKEY_CURRENT_USER, WEBVIEW2_CLIENT_KEY),
    )
    for hive, path in candidates:
        try:
            with winreg.OpenKey(hive, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
            # Microsoft documents "0.0.0.0" as explicitly meaning "not
            # installed" rather than absent — treated as absent here.
            if isinstance(version, str) and version and version != "0.0.0.0":
                return version
        except OSError:
            continue
    return None


def webview2_status() -> RuntimeStatus:
    if sys.platform != "win32":
        return RuntimeStatus(
            name="WebView2 Runtime",
            available=False,
            applicable=False,
            detail="Only used on Windows.",
        )

    version = _read_webview2_version()
    if version:
        return RuntimeStatus(
            name="WebView2 Runtime",
            available=True,
            detail=f"Installed (version {version}).",
        )
    return RuntimeStatus(
        name="WebView2 Runtime",
        available=False,
        detail=WEBVIEW2_MISSING_MESSAGE,
        fix_url=WEBVIEW2_DOWNLOAD_URL,
    )


def dotnet_status() -> RuntimeStatus:
    """Probes pythonnet rather than reading a version key.

    What matters is not "is some .NET present" but "can this build load
    the CLR", and the only honest way to answer that is to try. The
    import is the same one pywebview's WinForms backend performs.
    """
    if sys.platform != "win32":
        return RuntimeStatus(
            name=".NET runtime",
            available=False,
            applicable=False,
            detail="Only used on Windows.",
        )
    try:
        import clr  # noqa: F401 — the import *is* the probe
    except Exception as exc:  # noqa: BLE001
        logger.warning("pythonnet/.NET is not usable: %s", type(exc).__name__)
        return RuntimeStatus(name=".NET runtime", available=False, detail=DOTNET_MISSING_MESSAGE)
    return RuntimeStatus(name=".NET runtime", available=True, detail="Available.")


def window_runtime_error() -> Optional[str]:
    """The IPC error detail for the first missing dependency, or None
    when the window should be able to start.

    Ordered deliberately: WebView2 is the one a user can fix themselves
    in a minute, so it is reported first when both are missing.
    """
    from app.launcher import ipc

    if sys.platform != "win32":
        return None
    if not webview2_status().available:
        return ipc.ERROR_WEBVIEW2_MISSING
    if not dotnet_status().available:
        return ipc.ERROR_DOTNET_MISSING
    return None


def describe(error_detail: str) -> str:
    """A user-facing sentence for an IPC error detail.

    Total, and deliberately a pure lookup: an unrecognised detail still
    produces something honest rather than a KeyError in an error path,
    and no branch re-probes the machine (see the message constants above
    for why).
    """
    from app.launcher import ipc

    if error_detail == ipc.ERROR_WEBVIEW2_MISSING:
        return WEBVIEW2_MISSING_MESSAGE
    if error_detail == ipc.ERROR_DOTNET_MISSING:
        return DOTNET_MISSING_MESSAGE
    return WINDOW_FAILED_MESSAGE


def fix_url_for(error_detail: str) -> Optional[str]:
    """The page that fixes *error_detail*, when one exists. Only WebView2
    has a download a user can act on; a broken .NET/pythonnet load is a
    repair-JARVIS problem, not a go-download-this one."""
    from app.launcher import ipc

    if error_detail == ipc.ERROR_WEBVIEW2_MISSING:
        return WEBVIEW2_DOWNLOAD_URL
    return None
