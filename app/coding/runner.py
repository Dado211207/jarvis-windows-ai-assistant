"""Running a classified command, and being able to prove it stopped.

`app/coding/commands.py` decides *whether*; this module decides nothing
and executes what it is handed. The split is deliberate: a reviewer
checking "can this run PowerShell" reads one file, and a reviewer
checking "can this leak a process" reads the other.

**argv only.** `subprocess.Popen` is called with a list and
`shell=False`, always. There is no code path here that builds a command
string, so there is no code path where a shell metacharacter could be
interpreted. `tests/test_security_invariants.py::test_no_subprocess_call_uses_a_shell`
AST-walks this file along with every other and would fail the build if
that ever changed.

**A minimal environment.** The child does not inherit this process's
environment wholesale. It gets PATH, the handful of variables a
toolchain genuinely needs, and nothing else — no `ANTHROPIC_API_KEY`,
no `ELEVENLABS_*`, no session token, no AWS or GitHub credentials that
happen to be exported in the parent shell. A build tool has no business
reading those, and "it probably won't" is not a control.

**Ownership, not name matching.** Every process started here is captured
as a `ProcessIdentity` — PID *plus* creation time — using the same module
that fixed the WebView2 leak. Stopping a command terminates the tree
descended from *the process we started*, verified immediately before it
is signalled. An unrelated `node` the user is running is never a target,
because it was never captured.

**Output is bounded and redacted before it is stored.** A build that
prints a megabyte of warnings is truncated with an explicit marker, not
silently. Every line goes through `redaction.redact_message()` on the way
out, so a token echoed by a misbehaving tool does not land in a task
record or an event.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from app.coding import limits
from app.core.redaction import redact_message
from app.launcher import process_tree
from app.logging_config import get_logger

logger = get_logger("coding.runner")

# Variables a development toolchain genuinely needs. Everything else in
# this process's environment is dropped.
_ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
    "NODE_PATH", "NPM_CONFIG_PREFIX", "PYTHONPATH", "VIRTUAL_ENV",
)

# Values a caller may set explicitly, which are not read from os.environ.
# The preview needs all three; none of them can carry a credential, and
# keeping them separate from _ENV_ALLOWLIST means "inherited from this
# machine" and "chosen by JARVIS" stay distinguishable.
_ENV_SETTABLE = ("PORT", "HOST", "BROWSER", "FORCE_COLOR", "npm_config_registry")

# Forced into every child. `CI=1` stops interactive prompts; the npm and
# pip flags stop a package manager opening a browser or waiting on input.
_ENV_FORCED = {
    "CI": "1",
    "NO_COLOR": "1",
    "npm_config_yes": "false",       # npx must never auto-install silently
    "npm_config_audit": "false",
    "npm_config_fund": "false",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def _identity_for_pid(pid: Optional[int]) -> Optional["process_tree.ProcessIdentity"]:
    """The identity (PID *plus* creation time) of a live PID.

    Returns None when psutil is unavailable or the process has already
    gone: an identity without a creation time is unverifiable, and
    process_tree deliberately refuses to signal an unverifiable target.
    """
    if pid is None:
        return None
    try:
        import psutil  # local import: psutil is optional at runtime
    except ImportError:
        return None
    try:
        return process_tree.identity_of(psutil.Process(pid))
    except Exception:  # noqa: BLE001 — already gone, or not inspectable
        return None


@dataclass
class CommandOutcome:
    argv: List[str]
    cwd: str                      # project-relative, never absolute
    started_at: float
    finished_at: float
    exit_code: Optional[int]
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    cancelled: bool
    cleanup: Optional[dict] = None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled

    def as_dict(self) -> dict:
        return {
            "argv": self.argv,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "cleanup": self.cleanup,
        }


def build_environment(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The child's environment: an allowlist, plus forced safety flags.

    Deliberately not `os.environ.copy()`. See the module docstring.
    """
    env: Dict[str, str] = {}
    for name in _ENV_ALLOWLIST:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env.update(_ENV_FORCED)
    for key, value in (extra or {}).items():
        # An override may not reintroduce something the allowlist dropped
        # under a different name; only known-safe keys are accepted.
        if key in _ENV_ALLOWLIST or key in _ENV_FORCED or key in _ENV_SETTABLE:
            env[key] = value
        else:
            # Refusing is right; refusing *silently* is not. A caller that
            # passed a key believes it arrived, and the only way to find
            # out otherwise was to inspect the child. Say so.
            logger.warning(
                "Ignoring environment variable %r for a coding command: it is not "
                "one this runner is allowed to set. Add it to _ENV_SETTABLE if it "
                "is genuinely safe.", key,
            )
    return env


def redacted_environment_summary(env: Dict[str, str]) -> Dict[str, str]:
    """What may be recorded about the environment a command ran with:
    the names, and for PATH-like values a count. Never a value."""
    summary: Dict[str, str] = {}
    for key, value in sorted(env.items()):
        if key in ("PATH", "PATHEXT", "PYTHONPATH", "NODE_PATH"):
            summary[key] = f"<{len(value.split(os.pathsep))} entries>"
        elif key in _ENV_FORCED:
            summary[key] = value
        else:
            summary[key] = "<set>"
    return summary


class CommandHandle:
    """A running command, and the means to stop it completely.

    Stop is not "send a signal and hope". It captures the descendants
    while the parent is still alive, signals the whole set, waits, and
    reports what actually happened to each one.
    """

    def __init__(self, argv: Sequence[str], cwd: Path, display_cwd: str) -> None:
        self.argv = [str(a) for a in argv]
        self.cwd = cwd
        self.display_cwd = display_cwd
        self._process: Optional[subprocess.Popen] = None
        # The root's own identity, captured while it is certainly alive.
        # A subprocess.Popen is NOT a psutil.Process and cannot answer
        # create_time, so building this at stop() time — when the process
        # may also already be gone — was how an earlier version of this
        # module threw inside cleanup and left the child running.
        self._own: Optional[process_tree.ProcessIdentity] = None
        self._captured: List[process_tree.ProcessIdentity] = []
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._cleanup: Optional[dict] = None

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self._process is not None else None

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def _resolved_argv(self, env: Dict[str, str]) -> List[str]:
        """The same argv, with the program resolved to an absolute path.

        **This is a Windows correctness fix, not a tidiness one.** On
        Windows `npm`, `npx`, `yarn` and `pnpm` are `.cmd` shims, and
        `CreateProcess` — which is what `Popen(..., shell=False)` calls —
        does **not** apply `PATHEXT`. So `["npm", "run", "dev"]` raises
        `FileNotFoundError` on every Windows machine, and the packaged
        acceptance test found exactly that: `app/coding/toolchain.py`
        reported `npm=available` (it resolves through `shutil.which`,
        which does apply PATHEXT) one line before the preview failed to
        start. Every Node project's dev server, lint, test and build
        command was unreachable on the platform this product ships for.

        Resolution uses the **child's** PATH, not this process's, so what
        is looked up is what the child would have looked up.

        `shell=False` and argv-only are untouched: this makes the program
        *more* explicit, never less. And a resolved path inside the
        project is refused for the same reason
        `toolchain._impersonation_refusal` refuses one — a repository does
        not get to supply the program that runs against it, and here it
        would actually be executed.

        That refusal is not decoration. `shutil.which` on Windows inserts
        `os.curdir` at the *front* of the search path — "the current
        directory takes precedence on Windows", in CPython's own words —
        so it resolves against this process's working directory before
        PATH. Nothing under `app/` calls `os.chdir` today, which is the
        only reason that directory is never the user's project; the
        containment check is what keeps this correct if that ever stops
        being true.
        """
        if not self.argv:
            return list(self.argv)

        program = self.argv[0]
        if os.sep in program or (os.altsep and os.altsep in program):
            return list(self.argv)          # already a path; nothing to look up

        # `path=""` rather than `path=None`: `shutil.which(cmd, path=None)`
        # falls back to *this* process's PATH, which would quietly resolve a
        # tool the child cannot see and contradict the paragraph above.
        found = shutil.which(program, path=env.get("PATH", ""))
        if not found:
            # Left as-is so Popen raises the FileNotFoundError callers
            # already turn into "'npm' is not installed, or is not on PATH".
            return list(self.argv)

        try:
            resolved = Path(found).resolve()
            root = self.cwd.resolve()
        except (OSError, RuntimeError):
            return [found, *self.argv[1:]]

        if resolved == root or root in resolved.parents:
            raise PermissionError(
                f"'{program}' resolved to a program inside this project. JARVIS "
                "will not run an executable a repository supplied."
            )
        return [str(resolved), *self.argv[1:]]

    def start(self, env: Dict[str, str]) -> None:
        creationflags = 0
        if os.name == "nt":
            # A new process group is what makes the whole tree signalable
            # as a unit, and CREATE_NO_WINDOW stops a console flashing up
            # over the user's work on every command.
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )

        self._process = subprocess.Popen(  # noqa: S603 — argv list, shell=False, see module docstring
            self._resolved_argv(env),
            cwd=str(self.cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._own = _identity_for_pid(self._process.pid)
        # Capture descendants shortly after start, and again before the
        # stop: a toolchain spawns its workers lazily, and one born after
        # the first capture is exactly the one that gets orphaned.
        self._recapture()

    def _recapture(self) -> None:
        if self._process is None:
            return
        with self._lock:
            fresh = process_tree.capture_descendants(self._process.pid)
            known = {(i.pid, i.create_time) for i in self._captured}
            for identity in fresh:
                if (identity.pid, identity.create_time) not in known:
                    self._captured.append(identity)

    def stop(self, reason: str = "stopped") -> dict:
        """Terminate the command and every process it started.

        Returns the structured cleanup report. Never raises: stopping is
        something the UI does under time pressure and it must not be able
        to fail in a way that leaves the caller stuck.
        """
        self._cancelled.set()
        self._recapture()

        report_dict: dict = {"reason": reason, "outcomes": {}, "survivors": []}
        try:
            targets = list(self._captured)
            if self._own is not None:
                # The root goes last: killing it first can reparent its
                # children and lose the relationship the capture relied on.
                targets = targets + [self._own]

            if targets:
                report = process_tree.terminate_identities(
                    targets,
                    terminate_grace_seconds=limits.TERMINATE_GRACE_SECONDS,
                    kill_grace_seconds=limits.KILL_GRACE_SECONDS,
                )
                # survivors/ok are properties on CleanupReport; outcomes and
                # summary are methods. Calling them the wrong way round threw
                # inside cleanup and left a timed-out child running — caught
                # by the runner's own tree test, which is why that test kills
                # a process that spawns a process.
                report_dict = {
                    "reason": reason,
                    "outcomes": report.outcomes(),
                    "survivors": [r.as_dict() for r in report.survivors],
                    "ok": report.ok,
                    "summary": report.summary(),
                }
        except Exception:  # noqa: BLE001 — cleanup must never raise
            logger.warning("Command cleanup raised; continuing.", exc_info=True)
            report_dict["error"] = "cleanup_failed"

        self._cleanup = report_dict
        return report_dict

    @property
    def cleanup_report(self) -> Optional[dict]:
        return self._cleanup


class _BoundedCapture:
    """Collects output up to a cap, redacting each line, and records that
    it truncated rather than pretending it did not."""

    def __init__(self, cap: int, on_line: Optional[Callable[[str, str], None]], stream: str) -> None:
        self.cap = cap
        self.parts: List[str] = []
        self.size = 0
        self.truncated = False
        self._on_line = on_line
        self._stream = stream

    def feed(self, line: str) -> None:
        safe = redact_message(line.rstrip("\n"))
        if self._on_line is not None:
            try:
                self._on_line(self._stream, safe)
            except Exception:  # noqa: BLE001 — a UI callback must not kill the command
                pass
        if self.truncated:
            return
        encoded = len(safe.encode("utf-8", errors="replace")) + 1
        if self.size + encoded > self.cap:
            # Keep the part of this line that fits rather than discarding
            # it whole. A minified bundle or a stack trace can be one
            # enormous line, and dropping it entirely turned a truncation
            # into "no output at all", which reads as a command that
            # printed nothing.
            remaining = max(0, self.cap - self.size)
            if remaining > 0:
                self.parts.append(safe.encode("utf-8", errors="replace")[:remaining]
                                  .decode("utf-8", errors="ignore"))
                self.size += remaining
            self.truncated = True
            self.parts.append(
                f"[output truncated at {self.cap:,} bytes — the command kept running]"
            )
            return
        self.size += encoded
        self.parts.append(safe)

    @property
    def text(self) -> str:
        return "\n".join(self.parts)


def run(
    argv: Sequence[str],
    cwd: Path,
    display_cwd: str,
    *,
    timeout_seconds: float = limits.DEFAULT_COMMAND_TIMEOUT_SECONDS,
    on_line: Optional[Callable[[str, str], None]] = None,
    handle: Optional[CommandHandle] = None,
) -> CommandOutcome:
    """Run one command to completion, a timeout, or a cancellation.

    The caller supplies `handle` when it needs to be able to stop the
    command from another thread — which the API does, because Stop has to
    work while this function is blocked reading output.
    """
    timeout_seconds = max(1.0, min(float(timeout_seconds), limits.MAX_COMMAND_TIMEOUT_SECONDS))
    handle = handle or CommandHandle(argv, cwd, display_cwd)
    env = build_environment()
    started = time.time()

    try:
        handle.start(env)
    except FileNotFoundError:
        return CommandOutcome(
            argv=list(argv), cwd=display_cwd, started_at=started, finished_at=time.time(),
            exit_code=None, stdout="",
            stderr=f"'{argv[0]}' is not installed, or is not on PATH.",
            truncated=False, timed_out=False, cancelled=False,
        )
    except PermissionError as exc:
        # A program the project supplied. Named rather than reduced to
        # "could not be started": refusing to run a repository's own
        # `npm.cmd` and failing to find npm at all call for entirely
        # different responses from the user.
        return CommandOutcome(
            argv=list(argv), cwd=display_cwd, started_at=started, finished_at=time.time(),
            exit_code=None, stdout="", stderr=str(exc),
            truncated=False, timed_out=False, cancelled=False,
        )
    except OSError as exc:
        return CommandOutcome(
            argv=list(argv), cwd=display_cwd, started_at=started, finished_at=time.time(),
            exit_code=None, stdout="",
            stderr=f"The command could not be started ({type(exc).__name__}).",
            truncated=False, timed_out=False, cancelled=False,
        )

    process = handle._process
    assert process is not None  # start() either set it or raised

    out = _BoundedCapture(limits.MAX_COMMAND_OUTPUT_BYTES, on_line, "stdout")
    err = _BoundedCapture(limits.MAX_COMMAND_OUTPUT_BYTES, on_line, "stderr")

    def pump(stream, sink: _BoundedCapture) -> None:
        try:
            for line in iter(stream.readline, ""):
                sink.feed(line)
        except (ValueError, OSError):
            pass  # the stream was closed under us by a stop
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    threads = [
        threading.Thread(target=pump, args=(process.stdout, out), daemon=True, name="jarvis-cmd-out"),
        threading.Thread(target=pump, args=(process.stderr, err), daemon=True, name="jarvis-cmd-err"),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    recaptured_at = time.monotonic()

    while True:
        if process.poll() is not None:
            break
        if handle.cancelled:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        # Re-capture periodically: a dev server's children appear after start.
        if time.monotonic() - recaptured_at > 1.0:
            handle._recapture()
            recaptured_at = time.monotonic()
        time.sleep(0.05)

    cleanup = None
    if timed_out or handle.cancelled or process.poll() is None:
        cleanup = handle.stop("timeout" if timed_out else "cancelled")

    for thread in threads:
        thread.join(timeout=2.0)

    exit_code = process.poll()
    finished = time.time()

    outcome = CommandOutcome(
        argv=list(argv),
        cwd=display_cwd,
        started_at=started,
        finished_at=finished,
        exit_code=exit_code,
        stdout=out.text,
        stderr=err.text,
        truncated=out.truncated or err.truncated,
        timed_out=timed_out,
        cancelled=handle.cancelled and not timed_out,
        cleanup=cleanup,
    )
    logger.info(
        "Coding command finished: %s in %s (exit=%s, timed_out=%s, cancelled=%s)",
        " ".join(outcome.argv[:3]), display_cwd or ".", exit_code, timed_out, outcome.cancelled,
    )
    return outcome


@dataclass
class ProcessLedger:
    """Every process tree this session owns, so Quit can end all of them.

    The launcher already terminates JARVIS's own children on shutdown;
    this exists so a *coding* command or preview is stopped deliberately
    and reported, rather than being left to whatever the process teardown
    happens to reach.
    """

    handles: List[CommandHandle] = field(default_factory=list)
    owners: Dict[int, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def track(self, handle: CommandHandle, owner_id: str = "") -> None:
        with self._lock:
            if handle not in self.handles:
                self.handles.append(handle)
            self.owners[id(handle)] = str(owner_id or "")

    def forget(self, handle: CommandHandle) -> None:
        with self._lock:
            if handle in self.handles:
                self.handles.remove(handle)
            self.owners.pop(id(handle), None)

    def stop_owner(self, owner_id: str, reason: str = "task stopped") -> List[dict]:
        owner = str(owner_id or "")
        with self._lock:
            handles = [h for h in self.handles if self.owners.get(id(h), "") == owner]
            for handle in handles:
                self.handles.remove(handle)
                self.owners.pop(id(handle), None)
        return [handle.stop(reason) for handle in handles]

    def stop_all(self, reason: str = "shutdown") -> List[dict]:
        with self._lock:
            handles = list(self.handles)
            self.handles.clear()
            self.owners.clear()
        return [handle.stop(reason) for handle in handles]

    def live_count(self, owner_id: Optional[str] = None) -> int:
        with self._lock:
            handles = list(self.handles)
            if owner_id is not None:
                owner = str(owner_id)
                handles = [h for h in handles if self.owners.get(id(h), "") == owner]
            return sum(
                1 for h in handles
                if h._process is not None and h._process.poll() is None
            )


ledger = ProcessLedger()
