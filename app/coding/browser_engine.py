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
from typing import Iterable, List, Optional, Tuple

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


#: Every Playwright layout we know, and whether it is a full browser.
#: `chrome-linux64` is what current builds unpack into; `chrome-linux` is
#: the older one. Only looking for the older layout once meant a freshly
#: installed Chromium was invisible and discovery fell through to whatever
#: `google-chrome` the machine happened to have — which is not the browser
#: anybody chose.
_CHROMIUM_LAYOUTS = (
    ("chrome-linux64/chrome", True),
    ("chrome-linux/chrome", True),
    ("chrome-headless-shell-linux64/chrome-headless-shell", False),
    ("chrome-linux/headless_shell", False),
)


def _build_number(directory_name: str) -> int:
    """The trailing build number of `chromium-1234`, or -1 if absent.

    Compared as an integer, never as a string: sorting names as text puts
    `chromium-999` above `chromium-1234`, which would pick an older
    browser the moment the counter gained a digit.
    """
    _, _, tail = directory_name.rpartition("-")
    return int(tail) if tail.isdigit() else -1


def _usable(path: Path) -> bool:
    """A real, executable file — not a directory or a half-unpacked stub.

    An interrupted `playwright install` leaves the directory tree behind
    without a working binary, and reporting that as the engine turns a
    missing browser into a crash at launch instead of a clear refusal.

    Windows has no execute bit, so `os.access(X_OK)` there is true for any
    regular file — which is the honest answer on that platform: a present
    `.exe` is launchable. The `is_file()` half is what rejects a directory
    standing where the binary should be, and that half works everywhere.
    """
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def chromium_search_roots() -> List[Path]:
    """Where a Playwright Chromium may live, most specific first.

    `PLAYWRIGHT_BROWSERS_PATH` is the documented override; `0` is its
    documented "install next to the package instead" value and is not a
    directory, so it is not treated as one.
    """
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    roots: List[Path] = []
    if configured and configured != "0":
        roots.append(Path(configured))
    roots.append(Path.home() / ".cache" / "ms-playwright")
    return roots


def select_chromium(roots: Iterable[Path]) -> Optional[str]:
    """The best Chromium under *roots*, or None if there is none.

    Selection is deterministic and ordered on two keys, in this order:

      1. **A full browser beats a headless shell.** Windows drives Edge,
         a full browser, and the two platforms should be running the same
         kind of thing. A shell is used only when there is no full
         browser at all.
      2. **Among equals, the newest build wins.** The earlier version of
         this preferred whichever *layout* matched first, so an old
         `chrome-linux` build beat a newer `chrome-linux64` one — the
         browser chosen depended on how it had been packaged rather than
         on what it was.

    This is a pure function of a directory tree, deliberately separate
    from `_chromium_for_ci`, which answers a different question — *should*
    we look at all — and answers "no" on Windows. Folding the two together
    made every ordering test above unrunnable on the only platform this
    product ships for: they called the gated entry point, it returned None
    before reading a single directory, and six of them failed on Windows
    CI while passing on Linux. That is the same defect `_stub_browser`
    exists to prevent, in the same file, one commit apart.
    """
    # (full_browser, build, path) — sorted once, over everything found, so
    # the answer cannot depend on which root or layout happened to be
    # visited first.
    found: List[Tuple[bool, int, str]] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for entry in root.glob("chromium*"):
                build = _build_number(entry.name)
                for relative, is_full in _CHROMIUM_LAYOUTS:
                    candidate = entry / relative
                    if _usable(candidate):
                        found.append((is_full, build, str(candidate)))
        except OSError:
            continue

    if not found:
        return None
    return max(found, key=lambda item: (item[0], item[1]))[2]


def _on_windows() -> bool:
    """The Windows guard, as one named predicate.

    Extracted so a test can assert the guard without assigning to
    `os.name`: `pathlib` reads that same attribute, so setting it to "nt"
    on Linux makes every `Path(...)` try to build a `WindowsPath` and
    raise `NotImplementedError`. A test that has to avoid touching the
    thing it is testing is a test waiting to become flaky.
    """
    return os.name == "nt"


def _chromium_for_ci() -> Optional[Engine]:
    """A Chromium for Linux CI. Never the answer on Windows.

    Kept deliberately separate so a reader can see that the product's
    Windows behaviour does not depend on a developer tool being present.
    """
    if _on_windows():
        return None

    path = select_chromium(chromium_search_roots())
    if path:
        return Engine("chromium", "Chromium", path)

    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        resolved = shutil.which(name)
        if resolved:
            return Engine("chromium", "Chromium", resolved)
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
        # `OptimizationHints` is deliberately NOT in this list, and that
        # is load-bearing.
        #
        # Disabling it leaves Chromium's optimisation guide half
        # initialised: its on-device model component update then fails and
        # the failure path dereferences null. Ten crashes in one CI run,
        # each preceded one line earlier by
        #
        #     Failed to update on-device model component with error 5
        #     Received signal 11 SEGV_MAPERR 000000000000
        #
        # one to one. Bisected against Chrome 151: the flag alone crashes,
        # removing it alone fixes it, and all six remaining entries are
        # innocent both alone and together. It had been in this list since
        # the browser checks were written, which is why ci.yml was red on
        # every commit from 5f4cdd4 onward while every local run passed —
        # the container here had an older Chromium without that component.
        #
        # Nothing is lost by leaving it enabled. `--host-resolver-rules`
        # below makes every host except the preview unresolvable, and
        # `--disable-background-networking` and `--disable-component-update`
        # are two lines up, so no hint can be fetched regardless. The two
        # OptimizationGuide entries that remain are the targeted ones: they
        # stop a machine-learning model being downloaded, which is the part
        # that actually mattered.
        "--disable-features=Translate,MediaRouter,"
        "InterestFeedContentSuggestions,CalculateNativeWinOcclusion,"
        "OptimizationGuideOnDeviceModel,OptimizationGuideModelDownloading",

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
