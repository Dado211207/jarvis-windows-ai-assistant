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

Three phases:
  A. Install, verify layout, launch the real exe, verify it serves real
     traffic, verify single-instance behavior, stop it gracefully.
  B. Silent-uninstall with no extra flags: verify removal, verify user
     data is preserved by default (the documented, safe default).
  C. Reinstall over the preserved data (proving the install-dir/data-dir
     separation actually protects data across a reinstall), then
     silent-uninstall again with the explicit /DELETEDATA=yes opt-in:
     verify user data is actually removed this time.
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

        _step("Phase A.9: Stop JARVIS gracefully (taskkill, no /F)")
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
    phase_b_uninstall_preserves_data_by_default(log_dir)
    phase_c_reinstall_then_explicit_data_removal(installer, log_dir)

    print("\nALL CLEAN-INSTALL CHECKS PASSED")


if __name__ == "__main__":
    main()
