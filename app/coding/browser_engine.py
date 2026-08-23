"""Finding and launching the Chromium engine that is already on this machine.

**The decision this file encodes.** Browser QA needs a real browser. Four
ways to get one were considered, and the evidence is in
`docs/browser-qa-architecture.md`:

1. Bundle Playwright and its own Chromium — ~137 MB of Playwright driver
   (124 MB of that is a private Node runtime) plus ~150 MB of browser, on
   top of a 101 MB installer.
2. Bundle Playwright and drive installed Edge — still the 137 MB driver.
3. Drive the WebView2 control pywebview already hosts — would navigate
   the user's own JARVIS window away, so it would need a second, hidden
   WebView2 host, which WebView2 does not officially support headless.
4. **Drive the already-installed Chromium-based runtime over the Chrome
   DevTools Protocol.** Zero new dependencies: `websockets` is already a
   transitive dependency through `uvicorn[standard]`, which
   `requirements.txt` has always declared. Zero installer growth. No
   download, ever.

Option 4 was chosen and measured before it was chosen: a spike performed
every check the brief requires — status, console errors, page exceptions,
failed requests, 4xx/5xx, `<h1>` count, title, language, broken images,
horizontal overflow, reduced motion, screenshot, accessibility tree — and
exited cleanly.

**What we drive.** Every candidate is Chromium and speaks the same
protocol:

* Microsoft Edge, present on every supported Windows 10 and 11
  installation.
* `msedgewebview2.exe` from the WebView2 Runtime, which JARVIS already
  requires and the installer already fetches — so even an Edge-stripped
  enterprise image has one.
* On Linux, a Chromium binary, for CI only.

**Discovery is bounded.** Registry `App Paths`, two known Program Files
locations, and `PATH`. No disk scan — the same rule
`app/core/legacy_migration.py` follows, for the same reason.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from app.logging_config import get_logger

logger = get_logger("coding.browser_engine")

# The WebView2 Runtime's client GUID — the same one
# app/launcher/runtime_check.py already probes for the window.
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


@dataclass(frozen=True)
class Engine:
    """A browser this machine already has."""

    kind: str            # "edge" | "webview2" | "chromium"
    display: str
    path: str
    version: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "display": self.display,
                "version": self.version, "path_is_recorded": False}


def _registry_app_path(executable: str) -> Optional[str]:
    """The App Paths entry Windows keeps for a registered program."""
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows only
        return None

    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (0, getattr(winreg, "KEY_WOW64_32KEY", 0)):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
                    value, _ = winreg.QueryValueEx(handle, "")
                if value and Path(value).is_file():
                    return str(value)
            except OSError:
                continue
    return None


def _webview2_version() -> Optional[str]:
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return None
    key = rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"
    for hive, path in (
        (winreg.HKEY_LOCAL_MACHINE, rf"Software\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE, key),
        (winreg.HKEY_CURRENT_USER, key),
    ):
        try:
            with winreg.OpenKey(hive, path) as handle:
                version, _ = winreg.QueryValueEx(handle, "pv")
            # Microsoft's documented "not installed" sentinel, defined in
            # app/launcher/runtime_check.py rather than repeated here.
            from app.launcher.runtime_check import NOT_INSTALLED_VERSION

            if isinstance(version, str) and version and version != NOT_INSTALLED_VERSION:
                return version
        except OSError:
            continue
    return None


def _webview2_binary() -> Optional[Engine]:
    """`msedgewebview2.exe` from the runtime JARVIS already requires."""
    version = _webview2_version()
    if not version:
        return None
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if not base:
            continue
        candidate = Path(base) / "Microsoft" / "EdgeWebView" / "Application" / version / "msedgewebview2.exe"
        if candidate.is_file():
            return Engine("webview2", "Microsoft Edge WebView2 Runtime", str(candidate), version)
    return None


def _edge() -> Optional[Engine]:
    registered = _registry_app_path("msedge.exe")
    if registered:
        return Engine("edge", "Microsoft Edge", registered)
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if not base:
            continue
        candidate = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if candidate.is_file():
            return Engine("edge", "Microsoft Edge", str(candidate))
    return None


def _chromium_for_ci() -> Optional[Engine]:
    """A Chromium for Linux CI. Never the answer on Windows.

    Kept deliberately separate so a reader can see that the product's
    Windows behaviour does not depend on a developer tool being present.
    """
    if os.name == "nt":
        return None

    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    roots: List[Path] = []
    if configured and configured != "0":
        roots.append(Path(configured))
    roots.append(Path.home() / ".cache" / "ms-playwright")
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for entry in sorted(root.glob("chromium-*"), reverse=True):
                for relative in ("chrome-linux/chrome", "chrome-linux/headless_shell"):
                    candidate = entry / relative
                    if candidate.is_file():
                        return Engine("chromium", "Chromium", str(candidate))
        except OSError:
            continue

    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return Engine("chromium", "Chromium", found)
    return None


def find_engine() -> Optional[Engine]:
    """The browser to use, or None.

    Order is deliberate. Edge first because it is a full browser kept
    current by Windows Update. The WebView2 runtime second because JARVIS
    already requires it, so it is the one candidate whose presence the
    installer guarantees. Chromium last and only off Windows.
    """
    for finder in (_edge, _webview2_binary, _chromium_for_ci):
        try:
            engine = finder()
        except Exception:  # noqa: BLE001 — discovery must never raise
            logger.debug("Browser discovery step failed.", exc_info=True)
            continue
        if engine is not None:
            return engine
    return None


def unavailable_reason() -> str:
    """Why there is no engine — with the step that fixes it."""
    if os.name == "nt":
        return (
            "No Chromium-based browser was found. JARVIS uses Microsoft Edge or the "
            "WebView2 Runtime, which Windows 10 and 11 normally include and the "
            "JARVIS installer offers to install. Repairing the WebView2 Runtime "
            "from Settings › Apps would restore browser checks."
        )
    return (
        "No Chromium binary was found. On this platform browser checks are a "
        "development-time capability; the packaged Windows application uses Edge "
        "or the WebView2 Runtime instead."
    )


def unavailable_fix() -> str:
    """The step that would make browser checks work on this machine."""
    if os.name == "nt":
        return (
            "Open Settings › Apps › Installed apps, find 'Microsoft Edge WebView2 "
            "Runtime', and choose Modify › Repair. Installing Microsoft Edge also "
            "works. JARVIS will not download either one."
        )
    return (
        "Install Chromium or Google Chrome, or set PLAYWRIGHT_BROWSERS_PATH to a "
        "directory containing one."
    )


def launch_argv(engine: Engine, *, debug_port: int, profile_dir: str,
                allow_host: str) -> List[str]:
    """The exact command line, and why each flag is on it.

    `--host-resolver-rules` is the important one: it makes every hostname
    except the owned preview fail to resolve *inside Chromium*, so an
    external redirect or subresource cannot leave this machine even if
    something upstream of it were wrong. That is enforcement by the
    browser rather than by our own bookkeeping.
    """
    argv = [
        engine.path,
        "--headless=new",
        f"--remote-debugging-port={debug_port}",
        # Bind the debugging endpoint to loopback. Anything else would put
        # full browser control on the network.
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",

        # Nothing may resolve except the preview we own.
        f"--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE {allow_host}",
        "--proxy-server=http://127.0.0.1:0",
        f"--proxy-bypass-list={allow_host}",

        # A clean, disposable browser with nothing of the user's in it.
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-plugins",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-client-side-phishing-detection",
        "--disable-default-apps",
        "--no-service-autorun",
        "--password-store=basic",
        "--use-mock-keychain",
        "--disable-features=Translate,OptimizationHints,MediaRouter,"
        "InterestFeedContentSuggestions,CalculateNativeWinOcclusion",

        # Determinism and safety.
        "--disable-gpu",
        "--hide-scrollbars",
        "--mute-audio",
        "--deny-permission-prompts",
        "--block-new-web-contents",          # no popups
        "--disable-popup-blocking=false",
        "--window-size=1280,900",
    ]
    if engine.kind == "webview2":
        # The WebView2 binary needs to be told it is being used standalone.
        argv.append("--embedded-browser-webview=0")
    if os.name != "nt":
        argv.append("--no-sandbox")
    return argv
