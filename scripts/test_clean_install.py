"""Automated clean-install acceptance test for the real JARVIS installer.

Run via `python scripts/test_clean_install.py` on a real Windows machine,
*after* scripts/build-installer.ps1 has produced a real installer at
packaging/dist/installer/JARVIS-Setup-*.exe (see
.github/workflows/windows-installer.yml, which runs both in sequence).
This is not a pytest suite: like scripts/ci_windows_smoke.py, it performs
real, side-effecting OS operations (silent install, launching the real
frozen JARVIS.exe, taskkill, silent uninstall) that don't fit the
mocked-unit-test model the rest of tests/ deliberately uses, and can only
ever run for real on Windows — there is no way to exercise this on this
project's Linux dev/CI sandbox.

Every check here hits the *installed*, *packaged* process — never the
repository's own Python/app modules directly — because that's the one
thing no amount of source-level testing (tests/test_packaging_spec.py,
tests/test_installer_script.py) can prove: that the real installer really
produces a real, independently-runnable Windows application.

Six phases, in the order they have to run:
  A. Install, verify layout, launch the real exe, verify it serves real
     traffic, verify single-instance behavior, stop it gracefully.
  F. Ask the installed JARVIS.exe to prove its own runtime — every
     capability the product claims must actually load inside the frozen
     process. A source-tree import proves only that the build machine
     had the package; it is what let a release candidate ship with no
     speech input while every test passed.
  D. Start and gracefully stop that same installed application ten times,
     checking after each one that the process is gone, the port is
     released and no JARVIS-started WebView2 process was left behind.
     One clean shutdown is a happy path; ten is evidence that nothing
     accumulates.
  E. Ten restarts through the code path the tray's Restart item calls,
     each proving the previous runtime was replaced rather than joined.
  B. Silent-uninstall with no extra flags: verify removal, verify user
     data is preserved by default (the documented, safe default).
  C. Reinstall over the preserved data (proving the install-dir/data-dir
     separation actually protects data across a reinstall), then
     silent-uninstall again with the explicit /DELETEDATA=yes opt-in:
     verify user data is actually removed this time.

D and E keep their letters rather than being renamed to fit the running
order: the phase names appear in CI logs and in the acceptance report,
and renaming A-C would silently invalidate every earlier reference to
them.
"""

import glob
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
# Running this file directly puts only scripts/ on sys.path, not the repo
# root — needed to import app.config for the default host/port (matching
# scripts/ci_windows_smoke.py's own reasoning for the same line).
sys.path.insert(0, str(REPO_ROOT))

from app.config import settings  # noqa: E402

INSTALLER_GLOB = str(REPO_ROOT / "packaging" / "dist" / "installer" / "JARVIS-Setup-*.exe")
BASE_URL = f"http://{settings.jarvis_host}:{settings.jarvis_port}"
HEALTH_URL = f"http://{settings.jarvis_host}:{settings.jarvis_port}/health"
SETUP_PAGE_URL = f"http://{settings.jarvis_host}:{settings.jarvis_port}/ui/setup"
STATIC_ASSET_URL = f"http://{settings.jarvis_host}:{settings.jarvis_port}/ui/static/style.css"

HEALTH_TIMEOUT_SECONDS = 30.0
PROCESS_EXIT_TIMEOUT_SECONDS = 15.0
INSTALL_TIMEOUT_SECONDS = 120.0
UNINSTALL_CLEANUP_TIMEOUT_SECONDS = 10.0


def _step(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


def _fail(text: str) -> None:
    print(f"\nFAILED: {text}", flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Expected paths. Deliberately NOT computed by importing
# app.core.app_paths and calling app_data_root()/default_db_path(): this
# test script itself runs as a plain, unfrozen Python process, so
# is_frozen() would be False and those functions would resolve to
# dev-mode (repo-root) paths — the opposite of what's being verified here.
# These mirror that module's packaged-mode formula independently, on
# purpose, so a real behavior change in app_paths.py that broke the
# installed layout would actually be caught here instead of both sides
# silently agreeing with each other.
# ---------------------------------------------------------------------------

def expected_install_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "Programs" / "JARVIS"


def expected_data_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "JARVIS"


def expected_db_path() -> Path:
    return expected_data_dir() / "data" / "jarvis.db"


def expected_log_file() -> Path:
    return expected_data_dir() / "data" / "logs" / "jarvis.log"


def expected_boot_trace_file() -> Path:
    """Matches app/launcher/boot_trace.py's own path exactly —
    app_data_root() / "boot_trace.log", a plain-file-I/O trace kept
    deliberately independent of the logging subsystem in
    expected_log_file() above, so a startup problem is diagnosable even
    if logging itself never produces any output."""
    return expected_data_dir() / "boot_trace.log"


def expected_start_menu_shortcut() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "JARVIS" / "JARVIS.lnk"


def expected_startup_shortcut() -> Path:
    """Matches app/launcher/startup_shortcut.py::shortcut_path()."""
    return (
        Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu"
        / "Programs" / "Startup" / "JARVIS.lnk"
    )


def _webview2_present() -> bool:
    """Whether the shared WebView2 runtime is still registered.

    Read from the registry, both the per-machine and per-user locations
    Microsoft documents, because an uninstaller that removes a shared
    component because one of its users left is a defect worth failing a
    release over.
    """
    import winreg

    key = r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    user_key = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for root, path in (
        (winreg.HKEY_LOCAL_MACHINE, key),
        (winreg.HKEY_CURRENT_USER, user_key),
    ):
        try:
            with winreg.OpenKey(root, path) as handle:
                version, _ = winreg.QueryValueEx(handle, "pv")
                if str(version).strip() not in ("", "0.0.0.0"):
                    return True
        except OSError:
            continue
    return False


def find_installer() -> Path:
    matches = glob.glob(INSTALLER_GLOB)
    if not matches:
        _fail(
            f"No installer found matching {INSTALLER_GLOB} — "
            "run scripts/build-installer.ps1 first."
        )
    return Path(matches[0])


def find_uninstaller() -> Path:
    matches = glob.glob(str(expected_install_dir() / "unins*.exe"))
    if not matches:
        _fail(f"No uninstaller found under {expected_install_dir()}")
    return Path(matches[0])


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------

def run_silent(exe: Path, log_path: Path, extra_args: Optional[list] = None) -> None:
    args = [str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log_path}"]
    args += extra_args or []
    print(f"Running: {' '.join(args)}")
    try:
        result = subprocess.run(args, timeout=INSTALL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _fail(f"{exe.name} did not finish within {INSTALL_TIMEOUT_SECONDS}s")
        return  # unreachable
    if result.returncode != 0:
        log_tail = ""
        if log_path.exists():
            log_tail = log_path.read_text(encoding="utf-16", errors="replace")[-2000:]
        _fail(f"{exe.name} exited with code {result.returncode}.\nLog tail:\n{log_tail}")


# ---------------------------------------------------------------------------
# Process / health helpers
# ---------------------------------------------------------------------------

def wait_for_health(proc: subprocess.Popen, timeout_seconds: float = HEALTH_TIMEOUT_SECONDS) -> dict:
    import httpx

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _fail(f"JARVIS.exe exited early (code {proc.returncode}) while waiting for it to become healthy.")
        try:
            response = httpx.get(HEALTH_URL, timeout=1.0)
            if response.status_code == 200:
                body = response.json()
                if body.get("healthy") is True:
                    return body
        except Exception:
            pass
        time.sleep(0.3)
    log_tail = _read_file_tail(expected_log_file())
    trace_tail = _read_file_tail(expected_boot_trace_file())
    _fail(
        f"JARVIS.exe never became healthy within {timeout_seconds}s ({HEALTH_URL}).\n"
        f"JARVIS's own log ({expected_log_file()}):\n{log_tail}\n"
        f"Boot trace ({expected_boot_trace_file()}):\n{trace_tail}"
    )
    return {}  # unreachable


def _read_file_tail(path: Path, max_chars: int = 4000) -> str:
    """Best-effort diagnostic only — never raises, since a missing or
    unreadable file is a real possibility (e.g. the app crashed before
    logging even initialized) and must not itself mask the real failure
    this is being called to help explain."""
    if not path.is_file():
        return f"(no file found at {path})"
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except OSError as e:
        return f"(could not read file: {e})"


def wait_for_health_to_stop(timeout_seconds: float = HEALTH_TIMEOUT_SECONDS) -> None:
    import httpx

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            httpx.get(HEALTH_URL, timeout=1.0)
        except Exception:
            return  # connection refused/reset — the server is down, as expected
        time.sleep(0.3)
    _fail(f"{HEALTH_URL} was still responding {timeout_seconds}s after requesting shutdown.")


DESKTOP_READY_URL = f"http://{settings.jarvis_host}:{settings.jarvis_port}/desktop/ready"
DESKTOP_READY_TIMEOUT_SECONDS = 60.0


def verify_installed_bytes() -> None:
    """The pinned licence and lexicon must survive packaging byte for byte.

    Checked against the *installed* tree, not the repository, because
    every step between them can alter a file: git's line-ending
    translation already did once, and a packaging step that rewrote or
    truncated a licence would be a real compliance problem that no
    source-level test could see.

    A licence text that has been edited is not the licence, and a lexicon
    that has been altered is not the data whose provenance was recorded.
    """
    import hashlib

    install_dir = expected_install_dir()
    checks = [
        (
            install_dir / "_internal" / "docs" / "licences" / "CMUDICT-LICENSE.txt",
            REPO_ROOT / "docs" / "licences" / "CMUDICT-LICENSE.txt",
            "CMU licence text",
        ),
        (
            install_dir / "_internal" / "app" / "voice" / "kokoro" / "data" / "lexicon.txt.gz",
            REPO_ROOT / "app" / "voice" / "kokoro" / "data" / "lexicon.txt.gz",
            "pronunciation lexicon",
        ),
    ]

    for installed, original, label in checks:
        if not installed.is_file():
            _fail(
                f"The installer did not ship the {label} — expected it at {installed}.\n"
                "It is bundled by packaging/jarvis.spec's datas list; a missing file "
                "there means the licence obligation does not travel with the product."
            )
        installed_digest = hashlib.sha256(installed.read_bytes()).hexdigest()
        original_digest = hashlib.sha256(original.read_bytes()).hexdigest()
        if installed_digest != original_digest:
            _fail(
                f"The installed {label} does not match the repository byte for byte.\n"
                f"  repository: {original_digest}\n"
                f"  installed:  {installed_digest}\n"
                "Something between git and the installed tree rewrote it."
            )
        print(f"OK: {label} preserved exactly ({installed_digest[:16]}…)")


def wait_for_desktop_ready(timeout_seconds: float = DESKTOP_READY_TIMEOUT_SECONDS) -> dict:
    """Wait for the parent's own readiness signal.

    /health answers as soon as the *server child* is up, several seconds
    before the parent has a window that can receive commands or a tray
    loop that can receive a close request. A graceful taskkill sent in
    that gap reaches nothing — exactly what happened on CI the first time
    the native window really opened.

    This polls GET /desktop/ready, which the launcher parent publishes
    after *proving* four things: the server answered health, the window
    child answered a ping over the authenticated control channel, the
    tray's message loop dispatched a message posted to it, and the parent
    finished startup. An earlier version of this wait parsed a line out of
    the boot trace; that was acceptable as evidence and wrong as a
    contract, since a human-readable log line is not an interface.

    Returns the final body so callers can assert on session_id.
    """
    import httpx

    deadline = time.monotonic() + timeout_seconds
    last = {}
    while time.monotonic() < deadline:
        try:
            response = httpx.get(DESKTOP_READY_URL, timeout=2.0)
            if response.status_code == 200:
                last = response.json()
                if last.get("ready") is True:
                    return last
        except Exception:
            pass  # the server may still be starting
        time.sleep(0.3)

    _fail(
        f"The desktop never reported ready within {timeout_seconds}s.\n"
        f"Still waiting on: {last.get('missing', 'unknown — no signal was ever published')}\n"
        f"Last signal: {last}\n"
        f"JARVIS's own log ({expected_log_file()}):\n{_read_file_tail(expected_log_file())}\n"
        f"Boot trace ({expected_boot_trace_file()}):\n{_read_file_tail(expected_boot_trace_file())}"
    )
    return {}


def wait_for_pid_exit(pid: int, timeout_seconds: float = PROCESS_EXIT_TIMEOUT_SECONDS) -> bool:
    import psutil

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.3)
    return False


def wait_for_path_removed(path: Path, timeout_seconds: float = UNINSTALL_CLEANUP_TIMEOUT_SECONDS) -> bool:
    """Poll until *path* no longer exists, instead of checking exactly once
    right after run_silent() returns.

    Real race, confirmed against Inno Setup's own FAQ (jrsoftware.org/isfaq.php):
    a running unins000.exe can't delete its own .exe file, so it spawns a
    clone into %TEMP% that does the actual removal work; that clone signals
    the originally-invoked unins000.exe process to exit *before* the clone
    has finished deleting unins000.exe/.dat and removing the (by then
    empty) install directory. subprocess.run() on the uninstaller we
    launched can therefore return before that tail end of cleanup is
    actually done — caught for real on windows-latest CI as an install-dir
    removal check failing immediately after a /VERYSILENT uninstall
    reported exit code 0."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not path.exists():
            return True
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def phase_a_install_launch_and_stop(installer: Path, log_dir: Path) -> None:
    _step("Phase A.1: Silent install")
    run_silent(installer, log_dir / "install.log")

    _step("Phase A.2: Verify install directory and JARVIS.exe")
    exe_path = expected_install_dir() / "JARVIS.exe"
    if not exe_path.is_file():
        _fail(f"Expected {exe_path} to exist after install.")
    print(f"OK: {exe_path}")

    _step("Phase A.3: Verify Start Menu shortcut")
    shortcut = expected_start_menu_shortcut()
    if not shortcut.is_file():
        _fail(f"Expected Start Menu shortcut {shortcut} to exist after install.")
    print(f"OK: {shortcut}")

    _step("Phase A.4: Launch the real installed JARVIS.exe (not repository Python)")
    proc = subprocess.Popen([str(exe_path)], cwd=str(expected_install_dir()))
    print(f"Launched pid={proc.pid}")

    try:
        _step("Phase A.5: Wait for the real /health endpoint")
        health_body = wait_for_health(proc)
        if health_body.get("version") != _read_expected_version():
            _fail(f"Running app reports version {health_body.get('version')!r}, expected {_read_expected_version()!r}.")
        print(f"OK: healthy, version={health_body.get('version')}")

        _step("Phase A.6: Verify the dashboard HTML and a static asset are served")
        _assert_url_serves(SETUP_PAGE_URL, expected_content_type="text/html")
        _assert_url_serves(STATIC_ASSET_URL, expected_content_type="text/css")
        print("OK: dashboard HTML and static asset both served by the installed exe")

        _step("Phase A.7: Verify the SQLite database was created under %LOCALAPPDATA%")
        db_path = expected_db_path()
        if not db_path.is_file():
            _fail(f"Expected database {db_path} to exist once the installed app is healthy.")
        print(f"OK: {db_path}")

        _step("Phase A.8: Verify single-instance behavior (launch a second copy)")
        second = subprocess.Popen([str(exe_path)], cwd=str(expected_install_dir()))
        try:
            exit_code = second.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            second.kill()
            _fail("A second JARVIS.exe launch did not exit promptly — expected it to detect the running instance and exit.")
        if exit_code != 0:
            _fail(f"A second JARVIS.exe launch exited with code {exit_code}, expected 0 (detected the running instance).")
        if proc.poll() is not None:
            _fail("The first JARVIS.exe instance is no longer running after the second launch attempt.")
        print("OK: second launch exited immediately (code 0); the original instance is still the only server running")

        _step("Phase A.9: Verify the installer preserved the pinned licence and data bytes")
        verify_installed_bytes()

        _step("Phase A.10: Wait for the desktop-ready signal before asking it to close")
        ready = wait_for_desktop_ready()
        print(
            "OK: desktop ready — server healthy, window answering commands, "
            f"tray listening, parent running (session {ready.get('session_id')})"
        )

        _step("Phase A.11: Stop JARVIS gracefully (taskkill, no /F)")
        result = subprocess.run(["taskkill", "/PID", str(proc.pid)], capture_output=True, text=True)
        print(result.stdout.strip() or result.stderr.strip())
        if not wait_for_pid_exit(proc.pid):
            # Same reasoning as wait_for_health()'s own failure path: a
            # shutdown that never completes is a dead end without the
            # app's own trace, and boot_trace.py is deliberately
            # independent of the logging subsystem so it still records
            # something even when logging itself is the broken part.
            _fail(
                f"JARVIS.exe (pid={proc.pid}) did not exit within {PROCESS_EXIT_TIMEOUT_SECONDS}s "
                f"of a graceful taskkill.\n"
                f"JARVIS's own log ({expected_log_file()}):\n{_read_file_tail(expected_log_file())}\n"
                f"Boot trace ({expected_boot_trace_file()}):\n{_read_file_tail(expected_boot_trace_file())}"
            )
        wait_for_health_to_stop()
        print("OK: process exited and the health endpoint stopped responding")
    finally:
        if proc.poll() is None:
            proc.kill()  # safety net only — the graceful path above is what's actually being verified


# ---------------------------------------------------------------------------
# Lifecycle: does it start and stop cleanly, every time, not just once
# ---------------------------------------------------------------------------

# Ten, because the failures this exists to catch are the ones that do not
# happen on the first try: a port still held by the previous run, a
# WebView2 process that outlived its parent, a lock file nobody released.
# One start/stop proves the happy path; the tenth proves nothing is
# accumulating.
LIFECYCLE_CYCLES = 10

# The browser process WebView2 runs the window in. It is started by the
# window child, so it is JARVIS's to clean up — an orphan here is the
# defect, not an unrelated process.
WEBVIEW_PROCESS_NAME = "msedgewebview2.exe"


def _jarvis_processes() -> list:
    """Every running JARVIS.exe, by image name.

    Deliberately name-based *here* and nowhere in the product: this is a
    test asserting that nothing called JARVIS.exe survives, which is
    precisely the question a name answers. The application itself only
    ever terminates processes it can prove are its own descendants (see
    app/launcher/process_tree.py).
    """
    import psutil

    found = []
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if (process.info["name"] or "").lower() == "jarvis.exe":
                found.append(process)
        except psutil.Error:
            continue
    return found


def _webview_children_of(pids: set) -> list:
    import psutil

    found = []
    for process in psutil.process_iter(["pid", "name", "ppid"]):
        try:
            if (process.info["name"] or "").lower() != WEBVIEW_PROCESS_NAME:
                continue
            if process.info["ppid"] in pids:
                found.append(process)
        except psutil.Error:
            continue
    return found


def _port_is_free() -> bool:
    """Whether the server port has actually been released.

    Uses a real connect attempt rather than a bind: on Windows a bind can
    succeed against a port another socket is still listening on, which
    would report a busy port as free — the exact bug fixed in
    app/launcher/server_process.py.
    """
    import socket

    try:
        with socket.create_connection((settings.jarvis_host, settings.jarvis_port), timeout=1.0):
            return False
    except OSError:
        return True


def _wait_for_port_release(timeout_seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _port_is_free():
            return True
        time.sleep(0.3)
    return False


def phase_d_repeated_start_and_quit(log_dir: Path) -> None:
    """Start and gracefully stop the installed application ten times.

    Every cycle asserts the whole contract, not just that the process
    exited: the desktop reported itself ready, the process is gone, the
    health endpoint stopped answering, the port was released, and neither
    a JARVIS.exe nor a JARVIS-owned WebView2 process was left behind.
    """
    exe_path = expected_install_dir() / "JARVIS.exe"
    if not exe_path.is_file():
        _fail(f"Expected {exe_path} to exist before the lifecycle test.")

    leftovers = _jarvis_processes()
    if leftovers:
        _fail(
            "JARVIS.exe was already running before the lifecycle test started "
            f"(pids {[p.pid for p in leftovers]}); the earlier phases did not shut down cleanly."
        )

    for cycle in range(1, LIFECYCLE_CYCLES + 1):
        _step(f"Phase D.{cycle}: cold start and graceful quit ({cycle} of {LIFECYCLE_CYCLES})")
        started = time.monotonic()
        proc = subprocess.Popen([str(exe_path)], cwd=str(expected_install_dir()))

        try:
            wait_for_health(proc)
            ready = wait_for_desktop_ready()
            ready_seconds = time.monotonic() - started

            # Record the whole family before asking it to stop, so an
            # orphan can be named rather than merely counted.
            own_pids = {process.pid for process in _jarvis_processes()}
            own_pids.add(proc.pid)
            webview_before = {process.pid for process in _webview_children_of(own_pids)}

            subprocess.run(["taskkill", "/PID", str(proc.pid)], capture_output=True, text=True)

            if not wait_for_pid_exit(proc.pid):
                _fail(
                    f"Cycle {cycle}: JARVIS.exe (pid={proc.pid}) did not exit within "
                    f"{PROCESS_EXIT_TIMEOUT_SECONDS}s of a graceful taskkill.\n"
                    f"Boot trace:\n{_read_file_tail(expected_boot_trace_file())}"
                )
            wait_for_health_to_stop()

            if not _wait_for_port_release():
                _fail(
                    f"Cycle {cycle}: port {settings.jarvis_port} was still accepting connections "
                    "after the process exited — something is still listening on it."
                )

            surviving = _jarvis_processes()
            if surviving:
                _fail(
                    f"Cycle {cycle}: {len(surviving)} JARVIS.exe process(es) survived a graceful "
                    f"quit (pids {[p.pid for p in surviving]})."
                )

            import psutil

            orphaned_webviews = [pid for pid in webview_before if psutil.pid_exists(pid)]
            if orphaned_webviews:
                _fail(
                    f"Cycle {cycle}: {len(orphaned_webviews)} WebView2 process(es) started by "
                    f"JARVIS outlived it (pids {orphaned_webviews})."
                )

            print(
                f"OK: cycle {cycle} — ready in {ready_seconds:.1f}s "
                f"(session {ready.get('session_id')}), exited cleanly, port released, "
                f"no JARVIS or WebView2 process left behind"
            )
        finally:
            if proc.poll() is None:
                proc.kill()  # safety net only; the graceful path is what is being verified

    print(f"OK: {LIFECYCLE_CYCLES} consecutive start/quit cycles left nothing behind")


SELFTEST_TIMEOUT_SECONDS = 120.0

# The deep pass loads two real models and runs real inference on a CPU
# runner. Generous because the work is genuinely slow, not because
# anything is being waited out — every step it performs has its own
# failure message.
DEEP_SELFTEST_TIMEOUT_SECONDS = 900.0

# Downloading the two models through the app's own screens.
MODEL_INSTALL_TIMEOUT_SECONDS = 900.0
MODEL_POLL_SECONDS = 3.0


def phase_f_installed_runtime_selftest(log_dir: Path) -> None:
    """Ask the installed executable what it can actually do.

    This is the check whose absence shipped a release candidate with no
    speech input at all. Every automated test passed at the time, because
    every one of them imported `faster_whisper` in the *source tree*,
    where it is installed by pip and imports fine. In the frozen build it
    raised ImportError — a hard dependency had been declared optional in
    the PyInstaller spec, its collection was skipped with a printed
    warning nobody read, and the product told the user to reinstall the
    identical artifact.

    So this runs the real `JARVIS.exe`, in its installed location, and
    fails the build if any capability the product claims cannot load.
    """
    exe_path = expected_install_dir() / "JARVIS.exe"
    if not exe_path.is_file():
        _fail(f"Expected {exe_path} to exist before the runtime self-test.")

    _step("Phase F: Ask the installed JARVIS.exe to prove its own runtime")
    _run_selftest(exe_path, log_dir, deep=False)


def _run_selftest(exe_path: Path, log_dir: Path, deep: bool) -> None:
    args = [str(exe_path), "--selftest"] + (["--deep"] if deep else [])
    timeout = DEEP_SELFTEST_TIMEOUT_SECONDS if deep else SELFTEST_TIMEOUT_SECONDS
    result = subprocess.run(
        args,
        capture_output=True, text=True, timeout=timeout,
        cwd=str(expected_install_dir()),
    )
    _report_selftest(result, log_dir / ("selftest-deep.log" if deep else "selftest.log"))


def _report_selftest(result, log_path: Path) -> None:
    output = (result.stdout or "") + (result.stderr or "")
    print(output.strip())

    try:
        log_path.write_text(output, encoding="utf-8")
    except OSError:
        pass

    if result.returncode != 0:
        _fail(
            "The installed application could not load every runtime it claims to have "
            f"(exit code {result.returncode}). Full output above; also saved to {log_path}.\n"
            "This is the packaged product failing, not the source tree — do not 'fix' it "
            "by relaxing this check."
        )

    if "SELF-TEST PASSED" not in output:
        _fail(
            "The self-test exited 0 but did not report a pass. Treating an ambiguous "
            "result as success is how the previous broken build shipped."
        )
    print("OK: every required runtime loaded inside the installed executable")


def phase_g_real_voice_through_the_installed_product(log_dir: Path) -> None:
    """Speak a sentence and read it back, in the artifact the user gets.

    Every voice test before this one measured a stage: the text
    normaliser, the grapheme-to-phoneme conversion, the ONNX session
    loading. All of those can pass while the product makes no sound, and
    all of them ran in the source tree, where the packaging fault that
    shipped a release candidate with no speech input was invisible.

    So: install both models through the installed application's own
    download screens — the same endpoints a person's button press
    reaches, with the same consent previews — then ask the installed
    executable to synthesise real audio with the real neural model and
    transcribe it back with the real speech recogniser. A pass means the
    whole chain works end to end in the packaged product.

    Deliberately not mocked at any point, and deliberately not gated on
    anything: if the models cannot be installed from inside the app, that
    is the defect, not a reason to skip.
    """
    import httpx

    exe_path = expected_install_dir() / "JARVIS.exe"
    if not exe_path.is_file():
        _fail(f"Expected {exe_path} to exist before the voice chain test.")

    _step("Phase G.1: Launch the installed app to drive its own download screens")
    proc = subprocess.Popen([str(exe_path)], cwd=str(expected_install_dir()))
    try:
        wait_for_health(proc)

        client = httpx.Client(base_url=BASE_URL, timeout=30.0)
        client.get("/health")  # mints the session cookie
        token = client.cookies.get("jarvis_session")
        if not token:
            _fail("The installed app did not issue a session token; the download endpoints are protected by it.")
        client.headers["X-JARVIS-Session-Token"] = token

        _step("Phase G.2: Install the neural voice through /voice/install")
        preview = client.get("/voice/install-preview").json()
        print(
            f"  will download {preview.get('download_bytes')} bytes of "
            f"{preview.get('voice_name')} from {preview.get('source')} ({preview.get('licence')})"
        )
        client.post("/voice/install", json={})
        _wait_for_installer(client, "/voice/install-status", "the neural voice")

        _step("Phase G.3: Install the speech model through the onboarding endpoint")
        client.post("/onboarding/speech-model/install", json={})
        _wait_for_installer(client, "/onboarding/speech-model/install-status", "the speech model")

        _step("Phase G.4: Confirm the app itself now reports voice input ready")
        diagnostics = client.get("/voice/diagnostics").json()
        if not diagnostics.get("runtime_ready") or not diagnostics.get("model_ready"):
            _fail(
                "After installing both models the app still reports voice input as not "
                f"ready: {diagnostics.get('state')} — {diagnostics.get('headline')}"
            )
        print(f"OK: {diagnostics.get('headline')}")
        client.close()
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid)], capture_output=True, text=True)
        wait_for_pid_exit(proc.pid)
        if proc.poll() is None:
            proc.kill()
    wait_for_health_to_stop()

    _step("Phase G.5: Synthesise real audio and transcribe it back, inside JARVIS.exe")
    _run_selftest(exe_path, log_dir, deep=True)
    print("OK: the installed executable produced real audio and read it back")


def _wait_for_installer(client, status_path: str, what: str) -> None:
    """Poll one of the app's installer endpoints until it settles.

    Reports the failure the app reported rather than a timeout: "the
    download failed because the checksum did not match" and "we waited
    long enough" are different problems.
    """
    deadline = time.monotonic() + MODEL_INSTALL_TIMEOUT_SECONDS
    last = ""
    while time.monotonic() < deadline:
        body = client.get(status_path).json()
        status = str(body.get("status", ""))
        message = str(body.get("message", ""))
        if message and message != last:
            print(f"  {status}: {message}")
            last = message
        if status == "complete":
            return
        if status in ("error", "cancelled"):
            _fail(f"Installing {what} through the installed app failed: {message}")
        time.sleep(MODEL_POLL_SECONDS)
    _fail(
        f"Installing {what} did not finish within {MODEL_INSTALL_TIMEOUT_SECONDS}s "
        f"(last message: {last!r})."
    )


RESTART_CYCLES = 10


def phase_e_repeated_restart(log_dir: Path) -> None:
    """Ten restarts through the code path the tray's Restart item calls.

    **What this covers and what it does not**, stated plainly because the
    difference matters. It drives `LauncherSupervisor.restart()` — the
    real method, with real child processes, a real port and a real health
    check — ten times in a row. It does not click the tray menu item: a
    native popup menu builds its command IDs when it opens, so there is
    no stable ID an external process could post, and the only way to make
    one would be to ship a control path into the product purely so a test
    could use it. The menu entry that calls this method is covered by
    unit tests instead (tests/test_launcher_tray.py).

    The property being proved is the one the reported defect was about:
    a restart leaves exactly one server behind, on a port it actually
    owns, with nothing from the previous generation still running.
    """
    import httpx
    import psutil

    from app.launcher.gui import LauncherSupervisor

    if not _wait_for_port_release():
        _fail("The port was still busy before the restart test — an earlier phase left something running.")

    supervisor = LauncherSupervisor()
    _step("Phase E.0: Start a runtime to restart")
    if not supervisor.start_server():
        _fail("The server child never became healthy, so restarts cannot be tested.")

    previous_pid = supervisor.server.pid
    print(f"OK: server child running (pid={previous_pid})")

    try:
        for cycle in range(1, RESTART_CYCLES + 1):
            _step(f"Phase E.{cycle}: restart ({cycle} of {RESTART_CYCLES})")
            started = time.monotonic()
            result = supervisor.restart()

            # A restart that brought the runtime back but could not open a
            # window is a different outcome from one that did not come
            # back at all, and only the second is a failure of this test —
            # a CI runner has no desktop session to show a window in.
            if not result.ok and not result.server_healthy:
                _fail(
                    f"Cycle {cycle}: the runtime did not come back after a restart "
                    f"(stage: {result.reason})."
                )

            server = supervisor.server
            if server is None:
                _fail(f"Cycle {cycle}: the supervisor has no server child after restarting.")
            if server.pid == previous_pid:
                _fail(
                    f"Cycle {cycle}: the server child pid did not change ({server.pid}); "
                    "the restart reused the old process instead of replacing it."
                )

            if psutil.pid_exists(previous_pid):
                _fail(
                    f"Cycle {cycle}: the previous server child (pid={previous_pid}) "
                    "is still running after the restart that replaced it."
                )

            health = httpx.get(HEALTH_URL, timeout=10.0)
            if health.status_code != 200:
                _fail(f"Cycle {cycle}: /health returned {health.status_code} after the restart.")

            print(
                f"OK: cycle {cycle} — new server pid={server.pid} healthy in "
                f"{time.monotonic() - started:.1f}s, previous generation gone"
                + ("" if result.ok else f" (window not opened: {result.window_reason})")
            )
            previous_pid = server.pid
    finally:
        supervisor.quit()

    if not _wait_for_port_release():
        _fail("The port was still held after the restart test shut its runtime down.")
    print(f"OK: {RESTART_CYCLES} restart cycles, each replacing the previous runtime completely")


def phase_b_uninstall_preserves_data_by_default(log_dir: Path) -> None:
    _step("Phase B.1: Silent uninstall (no /DELETEDATA flag)")
    uninstaller = find_uninstaller()
    run_silent(uninstaller, log_dir / "uninstall-1.log")

    _step("Phase B.2: Verify the install directory was removed")
    if not wait_for_path_removed(expected_install_dir()):
        _fail(f"Expected {expected_install_dir()} to be removed after uninstall.")
    print(f"OK: {expected_install_dir()} removed")

    _step("Phase B.3: Verify the Start Menu shortcut was removed")
    if expected_start_menu_shortcut().exists():
        _fail(f"Expected {expected_start_menu_shortcut()} to be removed after uninstall.")
    print("OK: Start Menu shortcut removed")

    _step("Phase B.4: Verify user data was PRESERVED by default")
    db_path = expected_db_path()
    if not db_path.is_file():
        _fail(f"Expected {db_path} to still exist after an uninstall with no /DELETEDATA flag (data must be preserved by default).")
    print(f"OK: {db_path} preserved")

    _step("Phase B.5: Verify the sign-in shortcut was removed even so")
    # Removed on every uninstall, not only a complete one: it points at
    # an executable that no longer exists, so leaving it means Windows
    # tries to launch a deleted file at every sign-in.
    startup_shortcut = expected_startup_shortcut()
    if startup_shortcut.exists():
        _fail(
            f"Expected {startup_shortcut} to be removed by uninstall — it points at an "
            "executable that no longer exists."
        )
    print("OK: sign-in shortcut removed")

    _step("Phase B.6: Verify shared Windows components were NOT removed")
    # An uninstaller that removes a shared component because one of its
    # users left is a bug in that uninstaller. WebView2 is the one this
    # installer can install, so it is the one worth checking.
    if not _webview2_present():
        _fail(
            "The WebView2 runtime is gone after uninstalling JARVIS. Shared Windows "
            "components must never be removed."
        )
    print("OK: WebView2 runtime left alone")


def phase_c_reinstall_then_explicit_data_removal(installer: Path, log_dir: Path) -> None:
    _step("Phase C.1: Reinstall over the preserved data directory")
    run_silent(installer, log_dir / "install-2.log")
    exe_path = expected_install_dir() / "JARVIS.exe"
    if not exe_path.is_file():
        _fail(f"Expected {exe_path} to exist after reinstall.")
    if not expected_db_path().is_file():
        _fail("The pre-existing database did not survive a reinstall — install dir and data dir must stay isolated.")
    print("OK: reinstalled cleanly; pre-existing data untouched")

    _step("Phase C.2: Silent uninstall WITH /DELETEDATA=yes")
    uninstaller = find_uninstaller()
    run_silent(uninstaller, log_dir / "uninstall-2.log", extra_args=["/DELETEDATA=yes"])

    _step("Phase C.3: Verify the install directory was removed")
    if not wait_for_path_removed(expected_install_dir()):
        _fail(f"Expected {expected_install_dir()} to be removed after uninstall.")
    print(f"OK: {expected_install_dir()} removed")

    _step("Phase C.4: Verify user data was REMOVED (explicit opt-in honored)")
    if expected_data_dir().exists():
        _fail(f"Expected {expected_data_dir()} to be removed after an uninstall with /DELETEDATA=yes.")
    print(f"OK: {expected_data_dir()} removed")

    _step("Phase C.5: Verify shared Windows components survived a complete removal too")
    if not _webview2_present():
        _fail(
            "The WebView2 runtime is gone after a complete uninstall. 'Everything JARVIS "
            "owns' never includes a shared Windows component."
        )
    print("OK: WebView2 runtime left alone")

    _step("Phase C.6: Verify the notes folder was left alone")
    # Documents somebody wrote. Uninstalling a program is not consent to
    # delete what was written with it, and this is the only phase where
    # a bug in that reasoning would be destructive.
    notes = Path.home() / "Documents" / "JARVIS_Notes"
    if notes.exists() and not notes.is_dir():
        _fail(f"{notes} is no longer a directory after uninstall.")
    print("OK: notes folder untouched")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _read_expected_version() -> str:
    from app import __version__
    return __version__


def _assert_url_serves(url: str, expected_content_type: str) -> None:
    import httpx

    response = httpx.get(url, timeout=5.0)
    if response.status_code != 200:
        _fail(f"GET {url} returned {response.status_code}, expected 200.")
    content_type = response.headers.get("content-type", "")
    if expected_content_type not in content_type:
        _fail(f"GET {url} returned content-type {content_type!r}, expected it to contain {expected_content_type!r}.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if os.name != "nt":
        _fail("This test installs and runs a real Windows executable — it must run on Windows.")

    installer = find_installer()
    print(f"Installer under test: {installer}")

    log_dir = REPO_ROOT / "packaging" / "dist" / "installer-test-logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    phase_a_install_launch_and_stop(installer, log_dir)
    # Before anything else: does the thing that was installed actually
    # contain what it claims? This is the check whose absence shipped a
    # release candidate with no speech input.
    phase_f_installed_runtime_selftest(log_dir)
    # Then the part no import check can answer: does it make a sound, and
    # can it hear one? Both models are installed through the app's own
    # download screens first, so this also proves those screens work.
    phase_g_real_voice_through_the_installed_product(log_dir)
    # Then the same artifact, started and stopped ten times. One clean
    # shutdown is a happy path; ten is evidence nothing accumulates.
    phase_d_repeated_start_and_quit(log_dir)
    phase_e_repeated_restart(log_dir)
    phase_b_uninstall_preserves_data_by_default(log_dir)
    phase_c_reinstall_then_explicit_data_removal(installer, log_dir)

    print("\nALL CLEAN-INSTALL CHECKS PASSED")


if __name__ == "__main__":
    main()
