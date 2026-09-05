"""Ask Windows directly about one process, and change nothing.

**Temporary scaffolding**, for the `msedge.exe` survivor investigation.
Remove it once that question is settled.

Why it exists
-------------
`process_tree` learns everything it knows about a process through psutil,
and two psutil behaviours make a survivor report hard to interpret:

* `Process.kill()` reaches `psutil_proc_kill`, which calls
  `TerminateProcess` and — verbatim from psutil 7.2.2's
  `arch/windows/proc.c` — **suppresses `ERROR_ACCESS_DENIED`**::

      if (!TerminateProcess(hProcess, SIGTERM)) {
          if (GetLastError() != ERROR_ACCESS_DENIED) {
              psutil_oserror_wsyscall("TerminateProcess");
              return NULL;
          }
      }

  So `kill_sent=True` with `kill_error=''` does **not** establish that the
  native call succeeded. It is equally consistent with `TerminateProcess`
  having failed with `ERROR_ACCESS_DENIED` and psutil returning success.

* `wait_procs` swallows `TimeoutExpired`, which is why `wait_error` is
  empty in every observed failure.

This module therefore asks the kernel, with no psutil in the path.

What it may and may not do
--------------------------
**Observation only.** It opens a handle for query and synchronise, reads
from it, and closes it. It never calls `TerminateProcess`, never signals
anything, never enumerates processes and never matches on an image name.
Sending another kill to collect evidence would change the very operation
under measurement, so it is not done.

One deliberate exception that is still not a kill: it tries to *open* a
handle that would grant `PROCESS_TERMINATE` and immediately closes it.
That answers "could a terminate have been permitted at all?" — the
`ERROR_ACCESS_DENIED` question above — without terminating anything.

Never raises. It runs on a shutdown path in a windowed build with no
console, where an unhandled exception becomes a dialog nobody can
dismiss. Every failure is reported as a value.

Every field is an integer, a bool or a short constant. Nothing here can
carry a path, a URL, a command line or a secret, which is the same
redaction rule the rest of `process_tree` follows.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, Optional

# Handle-open outcomes.
OPENED = "opened"
DENIED = "denied"
NOT_FOUND = "not_found"
OPEN_FAILED = "open_failed"

# Wait outcomes, kept distinct on purpose. "Timed out" and "the probe
# itself failed" are different facts and only one of them is evidence
# about the process.
SIGNALLED = "signalled"
TIMED_OUT = "timed_out"
WAIT_FAILED_ = "wait_failed"
NOT_PROBED = "not_probed"

STILL_ACTIVE = 259

_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_ABANDONED = 0x00000080
_WAIT_FAILED = 0xFFFFFFFF

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_SYNCHRONIZE = 0x00100000

_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87

# 100-nanosecond intervals between 1601-01-01 and 1970-01-01, so a
# FILETIME can be compared with psutil's epoch-seconds creation time.
_EPOCH_DELTA_100NS = 116444736000000000
_HUNDREDS_OF_NS_PER_SECOND = 10000000.0


def available() -> bool:
    return sys.platform == "win32"


def _blank(reason: str) -> Dict[str, Any]:
    return {
        "probe": reason,
        "open": "",
        "open_error": 0,
        "identity_matches": None,
        "wait": NOT_PROBED,
        "wait_error": 0,
        "exit_code": None,
        "exit_is_still_active": None,
        "terminate_access": "unknown",
        "probe_seconds": 0.0,
    }


def probe(pid: int, expected_create_time: Optional[float]) -> Dict[str, Any]:
    """Kernel-level facts about *pid*, verified against its creation time.

    `expected_create_time` is the value captured when the process was
    first identified. It is compared against the creation time read
    **from the opened handle**, not re-resolved by PID, so a match means
    this handle refers to the process we captured rather than to whatever
    now holds that number.
    """
    if not available():
        return _blank("not_windows")

    started = time.monotonic()
    record = _blank("ran")
    handle = None
    terminate_handle = None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        handle = k32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid
        )
        if not handle:
            error = ctypes.get_last_error()
            record["open_error"] = int(error)
            if error == _ERROR_ACCESS_DENIED:
                record["open"] = DENIED
            elif error == _ERROR_INVALID_PARAMETER:
                # Documented as "the PID is not there at all".
                record["open"] = NOT_FOUND
            else:
                record["open"] = OPEN_FAILED
            return record

        record["open"] = OPENED

        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if k32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            raw = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            seconds = (raw - _EPOCH_DELTA_100NS) / _HUNDREDS_OF_NS_PER_SECOND
            if expected_create_time is None:
                record["identity_matches"] = None
            else:
                record["identity_matches"] = abs(seconds - expected_create_time) < 0.5

        # Zero timeout: a question, not a wait. The cleanup's own budget
        # has already elapsed by the time this runs.
        code = k32.WaitForSingleObject(handle, 0)
        if code == _WAIT_OBJECT_0:
            record["wait"] = SIGNALLED
        elif code == _WAIT_TIMEOUT:
            record["wait"] = TIMED_OUT
        elif code == _WAIT_ABANDONED:
            record["wait"] = SIGNALLED
        else:
            record["wait"] = WAIT_FAILED_
            record["wait_error"] = int(ctypes.get_last_error())

        value = wintypes.DWORD()
        if k32.GetExitCodeProcess(handle, ctypes.byref(value)):
            record["exit_code"] = int(value.value)
            record["exit_is_still_active"] = (value.value == STILL_ACTIVE)

        # Could a terminate have been permitted? Opened and closed
        # immediately; nothing is terminated here.
        terminate_handle = k32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if terminate_handle:
            record["terminate_access"] = "grantable"
        else:
            error = ctypes.get_last_error()
            record["terminate_access"] = (
                "denied" if error == _ERROR_ACCESS_DENIED else f"unavailable_{int(error)}"
            )
        return record
    except Exception as exc:  # noqa: BLE001 — a diagnostic may never break shutdown
        record["probe"] = f"failed_{exc.__class__.__name__}"
        return record
    finally:
        try:
            if handle:
                import ctypes

                ctypes.WinDLL("kernel32").CloseHandle(handle)
            if terminate_handle:
                import ctypes

                ctypes.WinDLL("kernel32").CloseHandle(terminate_handle)
        except Exception:  # noqa: BLE001
            pass
        record["probe_seconds"] = round(time.monotonic() - started, 4)
