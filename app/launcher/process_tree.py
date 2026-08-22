"""Cleaning up the processes a child of ours left behind.

WebView2 hosts its browser and GPU processes as children of whatever
launched the control, and they do not necessarily die with it. Kill the
window child without dealing with them and the user sees "JARVIS" still
listed in Task Manager after choosing Quit — which is exactly the defect
reported from real Windows hardware.

Three rules this module never breaks:

* **Only our own descendants.** Targets are captured by walking down from
  a process *this launcher started*, never by matching on a name. A
  name-based sweep would happily terminate an unrelated msedge the user
  was browsing in.
* **Captured before the kill.** Once the parent is gone, the
  parent/child relationship that identifies its descendants is gone with
  it, so the capture has to happen while it is still alive.
* **A PID is not an identity.** See ProcessIdentity below.

Every function here is best-effort and total: psutil may be absent, a
process may vanish mid-walk, a PID may already have been recycled.
Tidying up leftovers must never be able to break shutdown, so nothing
raises and every wait is bounded.

**Why this module was rewritten.** The ten-cycle lifecycle test in
scripts/test_clean_install.py failed on a real Windows runner:

    === Phase D.2: cold start and graceful quit (2 of 10) ===
    FAILED: Cycle 2: 1 WebView2 process(es) started by JARVIS outlived it
    (pids [4296]).

Cycle 1 of the same run passed, and all ten cycles passed in a sibling
run of the identical commit. Three defects here explain that, and all
three are fixed below rather than one being guessed at:

1. **Nothing waited after kill().** terminate() was sent, a three-second
   grace was observed, kill() was sent — and the function returned
   immediately. Whether the process actually died was never established
   by anyone. A process that ignored TerminateProcess for three full
   seconds is precisely the one that needs a moment more, and precisely
   the one nobody was watching.
2. **Nothing reported what happened.** "Still alive after kill" and
   "exited cleanly" produced identical silence, so a failure left no
   evidence to diagnose it with — which is why cause (1) could only be
   inferred rather than read.
3. **Bare PIDs were terminated seconds after capture.** Windows recycles
   PIDs aggressively. Terminating a recycled PID does not merely produce
   a wrong log line; it kills a stranger's process while this module
   believes it is honouring its own first rule.
"""

import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Sequence

from app.logging_config import get_logger

logger = get_logger("launcher.process_tree")

TERMINATE_GRACE_SECONDS = 3.0
# The wait that did not exist. Symmetric with the terminate grace on
# purpose: a process that needs escalating to kill has already shown it
# is slow to go, so giving kill *less* time than terminate would be the
# wrong way round.
KILL_GRACE_SECONDS = 3.0

# Two readings of the same process's creation time can differ in the last
# decimal place between psutil calls. Anything beyond this is a different
# process wearing a recycled PID.
CREATE_TIME_TOLERANCE_SECONDS = 0.05

# Cleanup outcomes. Strings rather than an enum so they survive a trip
# through JSON into a log line unchanged.
GONE = "already_gone"
TERMINATED = "terminated"
KILLED = "killed"
STILL_ALIVE = "still_alive"
INACCESSIBLE = "inaccessible"
REUSED = "pid_reused"


@dataclass(frozen=True)
class ProcessIdentity:
    """Enough of a process to recognise it later — and deliberately no more.

    A bare PID is not an identity. Windows reuses PIDs freely, and this
    module captures its targets seconds before it acts on them: a grace
    period is exactly the window in which the PID it holds can come to
    mean something else. `create_time` is what turns a PID into an
    identity — two processes can share a PID, but they cannot share a PID
    *and* a creation timestamp.

    **No executable path, on purpose.** The image name
    ("msedgewebview2.exe") is everything a diagnostic needs. A full path
    on Windows contains the account name, and this record ends up in a
    log file.
    """

    pid: int
    create_time: Optional[float] = None
    name: str = ""
    ppid: Optional[int] = None

    def is_verifiable(self) -> bool:
        """Whether this identity can prove itself later.

        False means the creation time could not be read at capture time,
        so a later match would be a PID comparison wearing an identity's
        clothes.
        """
        return self.create_time is not None

    def matches(self, create_time: Optional[float]) -> bool:
        if self.create_time is None or create_time is None:
            return False
        return abs(self.create_time - create_time) <= CREATE_TIME_TOLERANCE_SECONDS


@dataclass
class CleanupResult:
    """What happened to one captured process, step by step.

    Every field is a bool, a float or an image name — there is nothing
    here to redact, which is what makes it safe to log in full.
    """

    identity: ProcessIdentity
    outcome: str
    alive_before: bool = False
    terminate_sent: bool = False
    exited_after_terminate: bool = False
    kill_sent: bool = False
    exited_after_kill: bool = False

    def as_dict(self) -> dict:
        data = asdict(self)
        data["identity"] = asdict(self.identity)
        return data


@dataclass
class CleanupReport:
    """The structured result of one cleanup pass.

    Returned rather than merely logged so a caller — and a test — can
    assert on what actually happened instead of scraping a log line.
    """

    results: List[CleanupResult] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def survivors(self) -> List[CleanupResult]:
        """The ones that are still running. An empty list is the contract
        being met; a non-empty one is the defect this module exists to
        prevent, reported honestly rather than swallowed."""
        return [result for result in self.results if result.outcome == STILL_ALIVE]

    @property
    def unknown(self) -> List[CleanupResult]:
        """The ones that could not be verified as ours, and so were
        deliberately left alone. Not a pass and not a failure — an
        unknown, which is why it is reported separately rather than being
        quietly folded into either."""
        return [result for result in self.results if result.outcome == INACCESSIBLE]

    @property
    def ok(self) -> bool:
        return not self.survivors

    def outcomes(self) -> dict:
        counts: dict = {}
        for result in self.results:
            counts[result.outcome] = counts.get(result.outcome, 0) + 1
        return counts

    def as_dict(self) -> dict:
        return {
            "duration_seconds": round(self.duration_seconds, 3),
            "outcomes": self.outcomes(),
            "processes": [result.as_dict() for result in self.results],
        }

    def summary(self) -> str:
        """One bounded line for a log file."""
        if not self.results:
            return "no leftover processes to clean up"
        counts = ", ".join(f"{name}={count}" for name, count in sorted(self.outcomes().items()))
        return f"{len(self.results)} leftover process(es) in {self.duration_seconds:.2f}s: {counts}"


def _psutil():
    """psutil is an optional convenience here, not a dependency shutdown
    is allowed to require."""
    try:
        import psutil

        return psutil
    except Exception:  # noqa: BLE001 — best-effort cleanup only
        return None


def identity_of(process) -> ProcessIdentity:
    """Build an identity from a live psutil.Process, tolerating a process
    that vanishes or refuses to answer halfway through."""

    def _safe(getter, default):
        try:
            return getter()
        except Exception:  # noqa: BLE001
            return default

    return ProcessIdentity(
        pid=process.pid,
        create_time=_safe(process.create_time, None),
        name=_safe(process.name, "") or "",
        ppid=_safe(process.ppid, None),
    )


def capture_descendants(pid: Optional[int]) -> List[ProcessIdentity]:
    """Identities of every process descended from *pid*, captured while
    it is still alive.

    The only way this module ever learns about a process. There is no
    enumeration and no name matching: the caller supplies a PID it
    started, and the walk goes downwards from there.
    """
    if pid is None:
        return []
    psutil = _psutil()
    if psutil is None:
        return []
    try:
        children = psutil.Process(pid).children(recursive=True)
    except Exception:  # noqa: BLE001 — already gone, or not ours to walk
        return []
    return [identity_of(child) for child in children]


def descendant_pids(pid: Optional[int]) -> List[int]:
    """PIDs descended from *pid*. Kept for callers and tests that only
    need the numbers; anything that intends to *act* on the result should
    use capture_descendants() so it holds identities instead."""
    return [identity.pid for identity in capture_descendants(pid)]


def _is_zombie(psutil, process) -> bool:
    """Whether this process has already exited but not yet been reaped.

    A POSIX process whose parent has not called wait() lingers as a
    zombie: `pid_exists()` says yes and `wait_procs()` will not report it
    as gone, even though it is dead and holds nothing. Treating one as a
    survivor would mean reporting a leak that does not exist.

    Windows — the platform this product ships on — has no zombies, so
    this is always False there. It matters on the Linux machines the
    tests run on, and getting it wrong there means the test suite
    disagrees with reality in the direction of false alarms.
    """
    try:
        return process.status() == psutil.STATUS_ZOMBIE
    except Exception:  # noqa: BLE001 — status is a convenience, never a requirement
        return False


def _resolve(psutil, identity: ProcessIdentity):
    """Find the live process this identity refers to, or say why not.

    Returns (process, outcome). A non-empty outcome means do not touch
    it: the process is gone, its PID now belongs to something else, or it
    cannot be verified as ours. **Refusing to act on an unverifiable
    process is deliberate.** The alternative is terminating on a PID
    alone, which is how a cleanup pass kills a stranger.
    """
    try:
        process = psutil.Process(identity.pid)
    except Exception:  # noqa: BLE001 — NoSuchProcess, or psutil unhappy
        return None, GONE

    if _is_zombie(psutil, process):
        return None, GONE

    try:
        create_time = process.create_time()
    except Exception:  # noqa: BLE001 — AccessDenied, or it just exited
        if not process.is_running():
            return None, GONE
        return None, INACCESSIBLE

    if not identity.is_verifiable():
        # Captured without a creation time, so there is nothing to check
        # it against. Treated as unverifiable rather than assumed ours.
        return None, INACCESSIBLE
    if not identity.matches(create_time):
        return None, REUSED
    return process, ""


def terminate_identities(
    identities: Sequence[ProcessIdentity],
    terminate_grace_seconds: float = TERMINATE_GRACE_SECONDS,
    kill_grace_seconds: float = KILL_GRACE_SECONDS,
    expand_descendants: bool = True,
) -> CleanupReport:
    """Terminate captured processes, escalate to kill, and *wait for the
    kill to land* — then report exactly what happened to each one.

    Bounded by construction: at worst one terminate grace plus one kill
    grace, whatever the processes do. JARVIS itself must always be able
    to close, so no path here can wait indefinitely.

    `expand_descendants` picks up helpers spawned after the original
    capture. WebView2 starts its renderer and GPU processes lazily, so a
    snapshot taken when the window child was asked to quit can be a
    process or two short by the time it exits. Anything added this way is
    a live descendant of a process already proven to be our descendant —
    the ownership rule still holds, transitively.
    """
    started = time.monotonic()
    report = CleanupReport()
    if not identities:
        return report

    psutil = _psutil()
    if psutil is None:
        report.results = [
            CleanupResult(identity=identity, outcome=INACCESSIBLE) for identity in identities
        ]
        report.duration_seconds = time.monotonic() - started
        return report

    targets: List[ProcessIdentity] = list(identities)
    if expand_descendants:
        seen = {identity.pid for identity in targets}
        for identity in list(targets):
            for extra in capture_descendants(identity.pid):
                if extra.pid not in seen:
                    seen.add(extra.pid)
                    targets.append(extra)

    live: List[tuple] = []  # (CleanupResult, psutil.Process)
    for identity in targets:
        process, outcome = _resolve(psutil, identity)
        if process is None:
            report.results.append(CleanupResult(identity=identity, outcome=outcome))
            continue
        result = CleanupResult(identity=identity, outcome=STILL_ALIVE, alive_before=True)
        report.results.append(result)
        live.append((result, process))

    if not live:
        report.duration_seconds = time.monotonic() - started
        _log(report)
        return report

    for result, process in live:
        try:
            process.terminate()
            result.terminate_sent = True
        except Exception:  # noqa: BLE001 — it may have exited between resolve and now
            continue

    live = _settle(psutil, live, terminate_grace_seconds, TERMINATED, "exited_after_terminate")

    for result, process in live:
        try:
            process.kill()
            result.kill_sent = True
        except Exception:  # noqa: BLE001
            continue

    # The wait that was missing. Without it "killed" meant "kill() did not
    # raise", which is not the same claim at all.
    live = _settle(psutil, live, kill_grace_seconds, KILLED, "exited_after_kill")

    report.duration_seconds = time.monotonic() - started
    _log(report)
    return report


def _settle(psutil, live: List[tuple], timeout: float, outcome: str, flag: str) -> List[tuple]:
    """Wait up to *timeout* for the given processes, marking the ones that
    exited and returning those still running."""
    if not live:
        return []
    processes = [process for _result, process in live]
    try:
        gone, _alive = psutil.wait_procs(processes, timeout=timeout)
    except Exception:  # noqa: BLE001 — a wait must never break shutdown
        return live

    finished = {process.pid for process in gone}
    remaining = []
    for result, process in live:
        if process.pid in finished or _is_zombie(psutil, process):
            result.outcome = outcome
            setattr(result, flag, True)
        else:
            remaining.append((result, process))
    return remaining


def _log(report: CleanupReport) -> None:
    """One bounded summary line, plus a line per process that did not
    resolve cleanly. Every field logged is a PID, an image name or a
    boolean — nothing here can carry a path, a URL or a secret."""
    if not report.results:
        return
    if report.ok and not report.unknown:
        logger.info("Leftover process cleanup: %s", report.summary())
        return

    logger.warning("Leftover process cleanup: %s", report.summary())
    for result in report.survivors:
        logger.warning(
            "  survived cleanup: pid=%s name=%s ppid=%s terminate_sent=%s kill_sent=%s",
            result.identity.pid,
            result.identity.name or "unknown",
            result.identity.ppid,
            result.terminate_sent,
            result.kill_sent,
        )
    for result in report.unknown:
        logger.warning(
            "  left alone, could not be verified as ours: pid=%s name=%s",
            result.identity.pid,
            result.identity.name or "unknown",
        )


def terminate_pids(pids: Sequence[int]) -> CleanupReport:
    """Convenience for callers holding bare PIDs.

    Captures each PID's identity *now* and delegates, so every stage
    after this call is reuse-protected. It cannot protect against a reuse
    that already happened before it was called — which is the reason the
    product's own shutdown paths capture identities up front rather than
    passing numbers around.
    """
    if not pids:
        return CleanupReport()
    psutil = _psutil()
    if psutil is None:
        return CleanupReport(
            results=[CleanupResult(identity=ProcessIdentity(pid=pid), outcome=INACCESSIBLE) for pid in pids]
        )

    identities = []
    for pid in pids:
        try:
            identities.append(identity_of(psutil.Process(pid)))
        except Exception:  # noqa: BLE001 — already gone
            identities.append(ProcessIdentity(pid=pid))
    return terminate_identities(identities)


def terminate_descendants_of(pid: Optional[int]) -> CleanupReport:
    """Capture-then-terminate in one call, for the common case where the
    parent is already gone and only its leftovers remain."""
    return terminate_identities(capture_descendants(pid))
