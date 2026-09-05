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

# Where a target came from. Diagnostic only — nothing branches on these,
# and they exist because "one msedge.exe survived" does not say whether
# the survivor was a process we captured up front or one we only noticed
# at cleanup time, and those are different defects.
FROM_CAPTURE = "captured"      # present in the sequence handed to us
FROM_EXPANSION = "expanded"    # found by expand_descendants, at cleanup time
FROM_ROOT = "root"             # the process the caller itself spawned


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

    #: Where this identity was learned from — diagnostic only.
    #: `compare=False` on purpose: this class is frozen and therefore
    #: hashable, and `recapture()` and the reuse check both rely on two
    #: identities for the same process comparing equal. A label about how
    #: we found it must not be able to make the same process look like a
    #: different one.
    source: str = field(default="", compare=False)

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

    # --- diagnostic fields ---
    #
    # Added because `still_alive` did not previously distinguish "this
    # process is running" from "wait_procs did not observe it exit", and
    # those are different claims. Nothing branches on any of these: they
    # are recorded, logged and reported, and the pass/fail contract
    # (`CleanupReport.survivors`) is unchanged.
    #
    # Every one is a short constant, a class name or a bool — the same
    # redaction rule the rest of this module follows. An exception's
    # *class* is safe; its `str()` is not, because a credential-store or
    # filesystem error can quote what it was looking for.
    source: str = ""
    terminate_error: str = ""
    kill_error: str = ""
    wait_error: str = ""
    #: Result of re-resolving PID + create_time *after* the kill grace.
    #: One of GONE / REUSED / INACCESSIBLE / STILL_ALIVE, or "" when the
    #: process never reached that stage.
    final_state: str = ""
    final_checked: bool = False

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
    # Which targets we were handed, and which we discovered ourselves.
    # Recorded rather than inferred: a survivor that was expanded at
    # cleanup time was never in the original capture, and a survivor that
    # was captured up front was. Those point at different defects.
    sources = {id(identity): identity.source or FROM_CAPTURE for identity in targets}
    if expand_descendants:
        seen = {identity.pid for identity in targets}
        for identity in list(targets):
            for extra in capture_descendants(identity.pid):
                if extra.pid not in seen:
                    seen.add(extra.pid)
                    sources[id(extra)] = FROM_EXPANSION
                    targets.append(extra)

    live: List[tuple] = []  # (CleanupResult, psutil.Process)
    for identity in targets:
        source = sources.get(id(identity), FROM_CAPTURE)
        process, outcome = _resolve(psutil, identity)
        if process is None:
            report.results.append(
                CleanupResult(identity=identity, outcome=outcome, source=source)
            )
            continue
        result = CleanupResult(
            identity=identity, outcome=STILL_ALIVE, alive_before=True, source=source,
        )
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
        except Exception as exc:  # noqa: BLE001 — it may have exited between resolve and now
            result.terminate_error = exc.__class__.__name__
            continue

    live = _settle(psutil, live, terminate_grace_seconds, TERMINATED, "exited_after_terminate")

    for result, process in live:
        try:
            process.kill()
            result.kill_sent = True
        except Exception as exc:  # noqa: BLE001
            result.kill_error = exc.__class__.__name__
            continue

    # The wait that was missing. Without it "killed" meant "kill() did not
    # raise", which is not the same claim at all.
    live = _settle(psutil, live, kill_grace_seconds, KILLED, "exited_after_kill")

    # And the check that was still missing after that. Reaching here means
    # `wait_procs` did not observe these processes exit within the shared
    # deadline — which is not the same claim as "this process is still
    # running", and until now the two were reported identically. Each
    # remaining target is re-resolved by PID *and* creation time, so the
    # four possible answers are told apart: it has gone, its PID now
    # belongs to something else, it cannot be verified, or it genuinely
    # survived.
    #
    # Diagnostic only. `result.outcome` is deliberately not rewritten from
    # this, so `CleanupReport.survivors` — and every caller and test that
    # depends on it — behaves exactly as before. Deciding what the outcome
    # *should* be needs evidence this check is being added to collect.
    for result, _process in live:
        result.final_checked = True
        result.final_state = _final_state(psutil, result.identity)

    report.duration_seconds = time.monotonic() - started
    _log(report)
    _record_diagnostics(report)
    return report


def _native_probe(identity: ProcessIdentity) -> dict:
    """Ask Windows about a survivor directly. Never raises, never signals.

    Import is local and guarded so that a module which is meaningless off
    Windows can never affect the shutdown path on any other platform, and
    so a missing or broken probe degrades to an empty record rather than
    to an exception during cleanup.
    """
    try:
        from app.launcher import process_probe

        if not process_probe.available():
            return {}
        return process_probe.probe(identity.pid, identity.create_time)
    except Exception as exc:  # noqa: BLE001 — a diagnostic may never break shutdown
        return {"probe": f"unavailable_{exc.__class__.__name__}"}


def _final_state(psutil, identity: ProcessIdentity) -> str:
    """Re-resolve one identity after the kill grace, and say what it is.

    Bounded by construction: a couple of psutil calls, no wait. Total, as
    everything in this module is — a diagnostic that could raise would
    break the shutdown it was added to explain.
    """
    try:
        process, outcome = _resolve(psutil, identity)
    except Exception:  # noqa: BLE001
        return INACCESSIBLE
    if process is not None:
        return STILL_ALIVE
    return outcome or INACCESSIBLE


def _settle(psutil, live: List[tuple], timeout: float, outcome: str, flag: str) -> List[tuple]:
    """Wait up to *timeout* for the given processes, marking the ones that
    exited and returning those still running."""
    if not live:
        return []
    processes = [process for _result, process in live]
    try:
        gone, _alive = psutil.wait_procs(processes, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — a wait must never break shutdown
        # Still never breaks shutdown, but no longer indistinguishable
        # from "the wait completed and nothing had exited". Those are
        # different facts and only one of them is evidence about the
        # process. The class name only — see CleanupResult.
        for result, _process in live:
            result.wait_error = exc.__class__.__name__
        logger.warning(
            "Leftover process cleanup: wait_procs raised %s over %d process(es); "
            "treating none as exited.",
            exc.__class__.__name__, len(live),
        )
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
            "  survived cleanup: pid=%s create_time=%s name=%s ppid=%s source=%s "
            "terminate_sent=%s terminate_error=%s kill_sent=%s kill_error=%s "
            "wait_error=%s final_state=%s",
            result.identity.pid,
            result.identity.create_time,
            result.identity.name or "unknown",
            result.identity.ppid,
            result.source or "unknown",
            result.terminate_sent,
            result.terminate_error or "none",
            result.kill_sent,
            result.kill_error or "none",
            result.wait_error or "none",
            result.final_state or "not_checked",
        )
    for result in report.unknown:
        logger.warning(
            "  left alone, could not be verified as ours: pid=%s name=%s",
            result.identity.pid,
            result.identity.name or "unknown",
        )


#: Name of the environment variable that turns the JSONL recorder on.
#: Unset — which is every shipped build and every ordinary test run —
#: means nothing is written and this module behaves exactly as before.
DIAGNOSTICS_ENV = "JARVIS_PROCESS_DIAGNOSTICS"


def _record_diagnostics(report: CleanupReport) -> None:
    """Append one JSON line per cleanup pass, when explicitly asked to.

    Temporary scaffolding for the Windows `msedge.exe` survivor
    investigation, and deliberately opt-in: a cleanup pass runs on the
    shutdown path of a windowed build with no console, and a diagnostic
    that writes to disk by default is a diagnostic that can fill one.

    Safe to write in full because `CleanupReport.as_dict()` is already
    built from PIDs, image names, PPIDs, timestamps, booleans and short
    constants — the redaction rule this module has always followed. No
    executable path is ever recorded, because none is ever captured.

    Total, like everything else here: a diagnostic must never be able to
    break the shutdown it exists to explain.
    """
    import os

    destination = os.environ.get(DIAGNOSTICS_ENV, "").strip()
    if not destination:
        return
    try:
        import json

        payload = report.as_dict()
        payload["recorded_at"] = time.time()
        payload["platform"] = os.name
        # Kernel-level corroboration for any survivor, recorded here
        # rather than on `CleanupResult` so that the report's shape — and
        # every assertion that pins it — is untouched.
        #
        # It is collected because two psutil behaviours make the existing
        # fields weaker evidence than they look: `psutil_proc_kill`
        # suppresses `ERROR_ACCESS_DENIED` from `TerminateProcess`, so
        # `kill_error=''` does not establish that the native call
        # succeeded; and `wait_procs` swallows `TimeoutExpired`, which is
        # why `wait_error` is empty in every observed failure.
        #
        # Observation only: it opens handles, reads, and closes them.
        # Sending another kill to collect evidence would change the very
        # operation being measured.
        survivors = [
            entry for entry in payload.get("processes", [])
            if entry.get("final_state") == STILL_ALIVE
        ]
        if survivors:
            payload["native_probes"] = [
                {
                    "pid": entry["identity"]["pid"],
                    "name": entry["identity"].get("name", ""),
                    "source": entry.get("source", ""),
                    "outcome": entry.get("outcome", ""),
                    "kill_sent": entry.get("kill_sent"),
                    "kill_error": entry.get("kill_error", ""),
                    "probe": _native_probe(
                        ProcessIdentity(
                            pid=entry["identity"]["pid"],
                            create_time=entry["identity"].get("create_time"),
                        )
                    ),
                }
                for entry in survivors
            ]
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 — never break shutdown for a diagnostic
        logger.debug("Could not record process diagnostics.", exc_info=True)


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
