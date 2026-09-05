"""Measure how long a terminated Windows process stays visible.

Diagnostic only. Imports nothing from `app/`, changes no production code,
and asserts nothing about the product. It exists to answer one question
that reading source cannot settle:

    After `TerminateProcess` has been called and the process object is
    signaled, for how long does the PID remain in `EnumProcesses` — and
    does holding an open handle prolong that?

Why the question decides anything
---------------------------------
`process_tree._settle()` marks a process gone only when
`psutil.wait_procs()` reports it gone. `wait_procs` calls
`Process.wait(timeout)` and swallows `TimeoutExpired`, which is why every
observed cleanup failure carries `wait_error=''`.

psutil 7.2.2's Windows `Process.wait()` (`_pswindows.py:888-897`) calls
`WaitForSingleObject`, and then — with its own comment reading "meaning
the process is gone" — refuses to return until `pid_exists()` is False.
`pid_exists` reaches `psutil_check_phandle`, which for an exit code other
than `STILL_ACTIVE` reports the process **running** whenever
`psutil_pid_in_pids(pid) == 1`; that is a scan of `EnumProcesses`.

So a process the kernel has confirmed dead is still reported alive by
psutil for as long as its PID is enumerated. If that interval can exceed
`KILL_GRACE_SECONDS = 3.0`, the cleanup reports `still_alive` for a
process that died on time. If it cannot, that explanation is dead and a
survivor report means a genuinely live process.

The measurement is the point. A narrative is not evidence.

Two arms, one control
---------------------
  held    — terminate, keep the handle open, poll until the PID clears
  closed  — terminate, close the handle at once, poll until the PID clears
  control — a live child, never terminated, polled the same way

The control must report itself alive throughout; an arm that reports the
control dead has a broken harness, and the run says so rather than
producing a number.

Windows only. On any other platform it reports that and exits 0, because
a diagnostic that cannot run must say so rather than appear to pass.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

# A controlled exit code, deliberately not 259 (STILL_ACTIVE), so
# "terminated" and "still running" can never be confused for one another.
VICTIM_EXIT_CODE = 42
STILL_ACTIVE = 259

# Long enough to cover the product's own worst case (3s terminate grace
# plus 3s kill grace) with headroom, short enough to stay bounded.
POLL_BUDGET_SECONDS = 20.0
POLL_INTERVAL_SECONDS = 0.01

WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_ABANDONED = 0x00000080
WAIT_FAILED = 0xFFFFFFFF

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000


def _wait_name(code: int) -> str:
    return {
        WAIT_OBJECT_0: "WAIT_OBJECT_0",
        WAIT_TIMEOUT: "WAIT_TIMEOUT",
        WAIT_ABANDONED: "WAIT_ABANDONED",
        WAIT_FAILED: "WAIT_FAILED",
    }.get(code, f"UNKNOWN_0x{code:08X}")


class Win32:
    """The handful of calls this needs, with results checked.

    Every wrapper reports failure explicitly rather than returning a
    value that could be mistaken for an answer. `WAIT_FAILED` is carried
    with its `GetLastError`, never folded into "timed out".
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self.k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.k32.WaitForSingleObject.restype = wintypes.DWORD
        self.k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.k32.GetExitCodeProcess.restype = wintypes.BOOL
        self.k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.k32.TerminateProcess.restype = wintypes.BOOL
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k32.CloseHandle.restype = wintypes.BOOL
        self.k32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        self.k32.GetProcessTimes.restype = wintypes.BOOL

    def open_process(self, pid: int, access: int) -> tuple:
        handle = self.k32.OpenProcess(access, False, pid)
        if not handle:
            return None, self.ctypes.get_last_error()
        return handle, 0

    def creation_time(self, handle) -> tuple:
        """Creation time read **from the handle itself**, not by PID.

        Reading it by PID would re-resolve through the very lookup whose
        behaviour is under test, and would say nothing about whether this
        handle still refers to the process we opened.
        """
        creation = self.wintypes.FILETIME()
        exit_ = self.wintypes.FILETIME()
        kernel = self.wintypes.FILETIME()
        user = self.wintypes.FILETIME()
        ok = self.k32.GetProcessTimes(
            handle,
            self.ctypes.byref(creation),
            self.ctypes.byref(exit_),
            self.ctypes.byref(kernel),
            self.ctypes.byref(user),
        )
        if not ok:
            return None, self.ctypes.get_last_error()
        value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return value, 0

    def wait(self, handle, milliseconds: int) -> tuple:
        code = self.k32.WaitForSingleObject(handle, milliseconds)
        error = self.ctypes.get_last_error() if code == WAIT_FAILED else 0
        return code, error

    def exit_code(self, handle) -> tuple:
        value = self.wintypes.DWORD()
        ok = self.k32.GetExitCodeProcess(handle, self.ctypes.byref(value))
        if not ok:
            return None, self.ctypes.get_last_error()
        return value.value, 0

    def terminate(self, handle, code: int) -> tuple:
        ok = self.k32.TerminateProcess(handle, code)
        if not ok:
            return False, self.ctypes.get_last_error()
        return True, 0

    def close(self, handle) -> bool:
        return bool(self.k32.CloseHandle(handle))


def _spawn_child() -> subprocess.Popen:
    """A child that simply sleeps. No product code, nothing to clean up."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _psutil_view(psutil, pid: int, expected_create: Optional[float]) -> Dict[str, Any]:
    """What psutil says, and separately what an EnumProcesses scan says.

    `pids()` membership is recorded as corroboration. On its own it is not
    treated as evidence of anything: the decisive comparison is between
    the kernel's own answer and psutil's.
    """
    view: Dict[str, Any] = {}
    try:
        view["pid_exists"] = bool(psutil.pid_exists(pid))
    except Exception as exc:  # noqa: BLE001
        view["pid_exists"] = None
        view["pid_exists_error"] = exc.__class__.__name__
    try:
        view["in_pids"] = pid in psutil.pids()
    except Exception as exc:  # noqa: BLE001
        view["in_pids"] = None
        view["in_pids_error"] = exc.__class__.__name__
    try:
        proc = psutil.Process(pid)
        view["resolvable"] = True
        try:
            actual = proc.create_time()
            view["create_time_matches"] = (
                None if expected_create is None else abs(actual - expected_create) < 0.0001
            )
        except Exception as exc:  # noqa: BLE001
            view["create_time_matches"] = None
            view["create_time_error"] = exc.__class__.__name__
        try:
            view["is_running"] = bool(proc.is_running())
        except Exception as exc:  # noqa: BLE001
            view["is_running"] = None
            view["is_running_error"] = exc.__class__.__name__
    except Exception as exc:  # noqa: BLE001
        view["resolvable"] = False
        view["resolve_error"] = exc.__class__.__name__
    return view


def _measure(arm: str, win: Win32, psutil) -> Dict[str, Any]:
    """One arm. Returns a record; never raises past its own cleanup."""
    record: Dict[str, Any] = {"arm": arm}
    child = _spawn_child()
    pid = child.pid
    record["pid"] = pid
    handle = None
    handle_closed = False
    try:
        access = PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        if arm != "control":
            access |= PROCESS_TERMINATE
        handle, err = win.open_process(pid, access)
        if handle is None:
            record["fatal"] = f"OpenProcess failed, GetLastError={err}"
            return record

        created, err = win.creation_time(handle)
        if created is None:
            record["fatal"] = f"GetProcessTimes failed, GetLastError={err}"
            return record
        record["handle_creation_time"] = created

        try:
            expected_create = psutil.Process(pid).create_time()
        except Exception:  # noqa: BLE001
            expected_create = None

        code, err = win.wait(handle, 0)
        record["wait_before"] = _wait_name(code)
        record["wait_before_error"] = err
        value, err = win.exit_code(handle)
        record["exit_code_before"] = value
        record["exit_code_before_is_still_active"] = (value == STILL_ACTIVE)
        record["psutil_before"] = _psutil_view(psutil, pid, expected_create)

        if arm == "control":
            # The control is never terminated. It exists to prove the
            # probes report a live process as live; if it does not, the
            # measured numbers from the other arms mean nothing.
            record["terminated"] = False
            time.sleep(0.5)
            code, err = win.wait(handle, 0)
            record["wait_after"] = _wait_name(code)
            record["wait_after_error"] = err
            value, _ = win.exit_code(handle)
            record["exit_code_after"] = value
            record["exit_code_after_is_still_active"] = (value == STILL_ACTIVE)
            record["psutil_after"] = _psutil_view(psutil, pid, expected_create)
            record["control_ok"] = bool(
                record["wait_after"] == "WAIT_TIMEOUT"
                and record["exit_code_after_is_still_active"]
                and record["psutil_after"].get("pid_exists") is True
            )
            return record

        ok, err = win.terminate(handle, VICTIM_EXIT_CODE)
        record["terminate_ok"] = ok
        record["terminate_error"] = err
        if not ok:
            record["fatal"] = f"TerminateProcess failed, GetLastError={err}"
            return record

        # TerminateProcess is asynchronous: it returns before the process
        # has finished exiting. Waiting for the object to signal is what
        # makes "the kernel says it is dead" a fact rather than an
        # assumption. Bounded; a wait that does not signal is reported.
        signalled_at = time.monotonic()
        code, err = win.wait(handle, int(POLL_BUDGET_SECONDS * 1000))
        record["wait_after_terminate"] = _wait_name(code)
        record["wait_after_terminate_error"] = err
        record["seconds_to_signal"] = round(time.monotonic() - signalled_at, 4)
        if code != WAIT_OBJECT_0:
            record["fatal"] = f"process did not signal: {_wait_name(code)}"
            return record

        value, err = win.exit_code(handle)
        record["exit_code_after"] = value
        record["exit_code_after_is_still_active"] = (value == STILL_ACTIVE)
        record["exit_code_is_the_one_we_set"] = (value == VICTIM_EXIT_CODE)

        if arm == "closed":
            handle_closed = win.close(handle)
            record["handle_closed_before_polling"] = handle_closed
            handle = None

        # The measurement. From the moment the kernel called it dead, how
        # long until psutil agrees?
        started = time.monotonic()
        cleared_pid_exists: Optional[float] = None
        cleared_in_pids: Optional[float] = None
        samples: List[Dict[str, Any]] = []
        while time.monotonic() - started < POLL_BUDGET_SECONDS:
            elapsed = round(time.monotonic() - started, 4)
            view = _psutil_view(psutil, pid, expected_create)
            if cleared_pid_exists is None and view.get("pid_exists") is False:
                cleared_pid_exists = elapsed
            if cleared_in_pids is None and view.get("in_pids") is False:
                cleared_in_pids = elapsed
            if len(samples) < 12:
                samples.append({"at": elapsed, **view})
            if cleared_pid_exists is not None and cleared_in_pids is not None:
                break
            time.sleep(POLL_INTERVAL_SECONDS)

        record["seconds_until_pid_exists_false"] = cleared_pid_exists
        record["seconds_until_absent_from_pids"] = cleared_in_pids
        record["polled_for_seconds"] = round(time.monotonic() - started, 4)
        record["samples"] = samples

        # The product's own bound. This is the number that decides whether
        # H1' can explain a `still_alive` report at all.
        grace = 3.0
        record["exceeds_kill_grace"] = (
            cleared_pid_exists is None or cleared_pid_exists > grace
        )
        return record
    finally:
        if handle is not None and not handle_closed:
            record["handle_closed_in_finally"] = win.close(handle)
        try:
            child.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            child.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    if platform.system() != "Windows":
        print(f"NOT RUN: this diagnostic is Windows-only; platform is {platform.system()}.")
        print("Reporting that rather than producing a number that would mean nothing.")
        return 0

    try:
        import psutil
    except Exception as exc:  # noqa: BLE001
        print(f"NOT RUN: psutil unavailable ({exc.__class__.__name__}).")
        return 0

    print(f"psutil {psutil.__version__} on {platform.platform()}")
    win = Win32()
    records: List[Dict[str, Any]] = []
    for index in range(args.repeat):
        for arm in ("control", "held", "closed"):
            record = _measure(arm, win, psutil)
            record["iteration"] = index
            records.append(record)
            summary = record.get("fatal") or (
                f"signal={record.get('wait_after_terminate')} "
                f"exit={record.get('exit_code_after')} "
                f"pid_exists_cleared_after={record.get('seconds_until_pid_exists_false')}s "
                f"absent_from_pids_after={record.get('seconds_until_absent_from_pids')}s "
                f"exceeds_3s_grace={record.get('exceeds_kill_grace')}"
                if arm != "control"
                else f"control_ok={record.get('control_ok')}"
            )
            print(f"  [{index}] {arm:<8} {summary}")

    controls = [r for r in records if r["arm"] == "control"]
    broken = [r for r in controls if not r.get("control_ok")]
    print()
    if broken:
        print(f"HARNESS INVALID: {len(broken)}/{len(controls)} controls did not report a live "
              "process as live. The other arms' numbers are not evidence.")
    else:
        print(f"Controls valid: {len(controls)}/{len(controls)} reported the live child as live.")

    for arm in ("held", "closed"):
        arm_records = [r for r in records if r["arm"] == arm and "fatal" not in r]
        if not arm_records:
            print(f"{arm}: no usable measurements.")
            continue
        cleared = [
            r["seconds_until_pid_exists_false"]
            for r in arm_records
            if r.get("seconds_until_pid_exists_false") is not None
        ]
        never = len(arm_records) - len(cleared)
        exceeded = sum(1 for r in arm_records if r.get("exceeds_kill_grace"))
        worst = max(cleared) if cleared else None
        print(
            f"{arm}: n={len(arm_records)} never_cleared={never} "
            f"exceeded_3s_grace={exceeded} worst_clear_time={worst}s"
        )

    print()
    print("H1' is supported only if a terminated process stays visible to psutil "
          "beyond the 3s kill grace. Anything less falsifies it, and a survivor "
          "report then means a genuinely live process.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        print(f"Wrote {len(records)} records to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
