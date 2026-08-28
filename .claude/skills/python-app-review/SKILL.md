---
name: python-app-review
description: Review a Python desktop application for Windows-specific correctness - executable resolution, subprocess safety, process lifecycle, single-instance behaviour, tray behaviour and localhost-only services. Use when reviewing Python desktop code, diagnosing a process that will not start or will not exit, or before packaging a build.
---

# Python desktop application review (Windows)

The defects below are the ones that survive code review on Linux and then break on a
user's Windows machine.

## 1. Executable resolution

Never assume a program is on `PATH`, and never assume a path that worked in
development exists on a user's machine.

- Resolve with `shutil.which(name)` and handle `None` as an expected outcome with a
  clear message, not a traceback.
- On Windows an executable may be `.exe`, `.cmd` or `.bat`. A `.cmd` shim cannot be
  executed by `CreateProcess` the way an `.exe` can — `shutil.which` finds it, but
  launching it may need the shell, which reintroduces quoting risk. Prefer resolving
  to the real `.exe` where one exists.
- Bundled resources: under PyInstaller, files live under `sys._MEIPASS` at runtime,
  not next to the script. Use a single helper:

  ```python
  def resource_path(relative: str) -> Path:
      base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
      return base / relative
  ```

- Writable data must **not** go next to the executable (`Program Files` is not
  writable for a standard user). Use `%LOCALAPPDATA%` / `%APPDATA%` via
  `os.environ` or `platformdirs`-style resolution, and create the directory.
- Long paths and paths containing spaces or non-ASCII characters must work. Test
  with a path containing a space and a non-ASCII character.

## 2. Subprocess safety

The rule: **arguments as a list, never a string; `shell=False`, always.**

```python
subprocess.run([exe, "--flag", user_value], shell=False, check=False,
               capture_output=True, text=True, timeout=30)
```

- `shell=True` with any interpolated value is command injection. A file name chosen
  by the user is untrusted input.
- Set a `timeout` on every call that can hang, and handle `TimeoutExpired` by killing
  the child, not by leaving it.
- Set `creationflags=subprocess.CREATE_NO_WINDOW` for background children in a GUI
  app, or a console window flashes on screen.
- Read stdout/stderr with `communicate()` or `capture_output=True`. A child that
  fills a pipe nobody drains deadlocks.
- Decode explicitly. Windows console output is often not UTF-8; pass `encoding` and
  `errors="replace"` rather than letting a decode error crash the app.
- Never pass a secret as a command-line argument — arguments are visible to any
  process listing. Use an environment variable scoped to the child, or stdin.

## 3. Process lifecycle

- Every spawned child has an owner that terminates it. On exit, terminate children
  explicitly; do not rely on the OS.
- `terminate()` then, after a bounded wait, `kill()`. Then `wait()` — a process you
  never `wait()` on stays as a zombie handle.
- Threads that outlive the window are the usual cause of "the app closed but is still
  in Task Manager". Make worker threads daemons **or** join them on shutdown; do not
  do neither.
- Register cleanup on the real exit paths: window close, tray quit, `SIGINT`, and an
  unhandled exception. `atexit` does not run on a hard kill.
- After quitting, no process from the app should remain. Verify with
  `tasklist /FI "IMAGENAME eq <name>.exe"` — see
  `/windows-ci-portability`.

## 4. Single-instance behaviour

If the app must run once at a time, pick a mechanism and handle every failure mode:

- a named mutex (`CreateMutexW`) is the Windows-native approach; check
  `ERROR_ALREADY_EXISTS` right after creating it
- a lock file must handle a **stale** lock left by a crash: store the PID, and check
  whether that PID is still alive and is actually this application before refusing to
  start
- a socket bound to a fixed localhost port doubles as a lock, but conflicts with any
  other program using that port

Whatever the mechanism: the second instance should surface the first (focus its
window) rather than exiting silently, and the lock must be released on crash.

## 5. Localhost-only services

If the app runs an HTTP or WebSocket server for its own UI:

- bind to `127.0.0.1`, never `0.0.0.0`. Binding to all interfaces exposes the service
  to the local network and triggers a Windows Firewall prompt.
- bind to port `0` and read the assigned port, rather than hard-coding one that may
  be taken.
- assume any local process can reach it: require a per-session token for anything
  that mutates state, and set a strict CORS policy. "It is only localhost" is not
  authentication.
- shut the server down on exit and confirm the port is released.

## 6. Tray applications

- a tray-only app must still be quittable from the tray menu, and that quit must run
  the same shutdown path as closing a window
- closing the last window must not silently leave the process running unless that is
  the documented behaviour, and the tray icon must then still be present
- the icon must be removed on exit, or a ghost icon remains until the user hovers it
- a tray app that starts hidden needs a discoverable way to show its window

## 7. Report

```
[executable|subprocess|lifecycle|single-instance|localhost|tray]  <file>:<line>
  Observed: <the code and what it does>
  Fails when: <the concrete Windows condition — no PATH entry, space in path,
              standard user, crash, second launch, network profile>
  Fix:      <the specific safer construction>
```

State clearly which findings you verified by running the app on Windows and which are
static review. Static review of Windows behaviour from a Linux session is inference,
and must be labelled as such.
