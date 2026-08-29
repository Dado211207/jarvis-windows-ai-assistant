"""Tests for app/launcher/process_tree.py.

Exercised against **real** child processes wherever the operating system
can produce the situation, not mocks. The whole point of this module is
what happens to processes that outlive their parent, and a mocked psutil
would prove nothing about that. Each test spawns short-lived `sleep`
processes through the current interpreter, so it works the same on Linux
CI and on a Windows runner.

Two things no real process can be made to do on demand — surviving a
kill, and psutil refusing to answer — are covered with a stand-in psutil
injected through `process_tree._psutil`. Those tests are about what this
module *reports* in that situation, which is exactly the part that had no
coverage when a WebView2 process outlived cycle 2 of the installer's
lifecycle test and left nothing behind to diagnose it with.
"""

import subprocess
import sys
import time

import pytest

psutil = pytest.importorskip("psutil")

from app.launcher import process_tree  # noqa: E402
from app.launcher.process_tree import ProcessIdentity  # noqa: E402


def _spawn_tree():
    """A parent that spawns one child and then waits — the shape WebView2
    produces, where the interesting processes are grandchildren of ours."""
    code = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "time.sleep(30)"
    )
    parent = subprocess.Popen([sys.executable, "-c", code])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process_tree.capture_descendants(parent.pid):
            return parent
        time.sleep(0.05)
    parent.kill()
    parent.wait(timeout=10)
    pytest.fail("the test child never spawned its own child")


def _sleeper(seconds=30):
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _code_only() -> str:
    """process_tree.py with its docstrings and comments removed.

    Mirrors tests/test_clean_install_script.py::_code_only(). Needed for
    any "this string must not appear" check, because this module's prose
    necessarily names the things it exists to never do.
    """
    import ast
    import inspect
    import io
    import tokenize

    source = inspect.getsource(process_tree)
    tree = ast.parse(source)

    # Line ranges occupied by docstrings, taken from the AST rather than
    # matched by content — a docstring is the first statement of a module,
    # class or function and nothing else is.
    prose_lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            prose_lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))

    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return "\n".join(
        token.string
        for token in tokens
        if token.type != tokenize.COMMENT and token.start[0] not in prose_lines
    )


def _cleanup(*processes):
    for process in processes:
        try:
            process_tree.terminate_descendants_of(process.pid)
            process.kill()
            process.wait(timeout=10)
        except Exception:
            pass


def _is_dead(pid) -> bool:
    """Whether the process is really gone.

    `pid_exists()` alone is not that question on Linux: a process killed
    while its parent is still running lingers as a zombie until the
    parent reaps it, and several trees here deliberately keep the parent
    alive. A zombie holds nothing and runs nothing — counting one as a
    survivor would fail the product for something the product did
    correctly. Windows, where this ships, has no zombies at all.
    """
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:
        return False


def _wait_gone(pid, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_dead(pid):
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Finding descendants
# ---------------------------------------------------------------------------

def test_descendants_of_a_live_process_are_found():
    parent = _spawn_tree()
    try:
        assert process_tree.capture_descendants(parent.pid), "a real child must be discoverable"
    finally:
        _cleanup(parent)


def test_a_captured_descendant_carries_a_verifiable_identity():
    """A PID alone cannot be checked against anything later. The creation
    time is what makes a later match a proof rather than a coincidence."""
    parent = _spawn_tree()
    try:
        captured = process_tree.capture_descendants(parent.pid)
        assert captured
        for identity in captured:
            assert identity.is_verifiable(), "captured without a creation time"
            assert identity.pid > 0
            assert identity.name, "an image name is needed to diagnose a survivor"
    finally:
        _cleanup(parent)


def test_a_captured_identity_never_carries_an_executable_path():
    """A full path on Windows contains the account name, and these records
    are written to a log file."""
    parent = _spawn_tree()
    try:
        identity = process_tree.capture_descendants(parent.pid)[0]
        fields = set(vars(identity))
        assert "exe" not in fields and "cmdline" not in fields and "cwd" not in fields
        # `source` is a short constant naming how we learned about the
        # process ("captured"/"expanded"/"root"), never a location.
        assert fields == {"pid", "create_time", "name", "ppid", "source"}
        assert identity.source in {"", process_tree.FROM_CAPTURE,
                                   process_tree.FROM_EXPANSION, process_tree.FROM_ROOT}
    finally:
        _cleanup(parent)


def test_a_process_with_no_children_reports_none():
    process = _sleeper()
    try:
        assert process_tree.capture_descendants(process.pid) == []
    finally:
        _cleanup(process)


@pytest.mark.parametrize("pid", [None, -1, 0x7FFFFFFF])
def test_an_impossible_pid_is_not_an_error(pid):
    """Best-effort cleanup: tidying up leftovers must never be able to
    break shutdown."""
    assert process_tree.capture_descendants(pid) == []
    assert process_tree.descendant_pids(pid) == []


def test_descendants_cannot_be_found_once_the_parent_is_gone():
    """The reason capture has to happen *before* the kill: the
    relationship that identifies our leftovers dies with the parent."""
    parent = _spawn_tree()
    captured = process_tree.capture_descendants(parent.pid)
    assert captured

    parent.kill()
    parent.wait(timeout=10)

    assert process_tree.capture_descendants(parent.pid) == []
    process_tree.terminate_identities(captured)


# ---------------------------------------------------------------------------
# Terminating them — and saying what happened
# ---------------------------------------------------------------------------

def test_captured_descendants_are_really_terminated():
    """The reported defect: after Quit, WebView2's processes were still
    listed in Task Manager as JARVIS."""
    parent = _spawn_tree()
    captured = process_tree.capture_descendants(parent.pid)
    parent.kill()
    parent.wait(timeout=10)

    report = process_tree.terminate_identities(captured)

    assert report.ok, f"leftover processes survived cleanup: {report.as_dict()}"
    for identity in captured:
        assert _wait_gone(identity.pid), f"pid {identity.pid} survived cleanup"


def test_a_descendant_that_exits_during_the_grace_period_is_reported_as_terminated():
    """The ordinary case: terminate is enough, and the report says so
    rather than claiming a kill that never happened."""
    process = _sleeper()
    identity = process_tree.identity_of(psutil.Process(process.pid))

    report = process_tree.terminate_identities([identity])

    assert len(report.results) == 1
    result = report.results[0]
    assert result.outcome == process_tree.TERMINATED
    assert result.alive_before and result.terminate_sent and result.exited_after_terminate
    assert not result.kill_sent, "kill must not be sent to a process that already went quietly"
    process.wait(timeout=10)


def test_a_descendant_that_ignores_terminate_is_killed_and_the_report_says_so():
    fake = _FakePsutil(survives_terminate=True)
    process_tree._psutil = lambda: fake  # noqa: SLF001 — injecting the stand-in
    try:
        identity = ProcessIdentity(pid=4296, create_time=1000.0, name="msedgewebview2.exe")
        report = process_tree.terminate_identities(
            [identity], terminate_grace_seconds=0.05, kill_grace_seconds=0.05
        )
    finally:
        process_tree._psutil = _REAL_PSUTIL_GETTER

    result = report.results[0]
    assert result.outcome == process_tree.KILLED
    assert result.terminate_sent and not result.exited_after_terminate
    assert result.kill_sent and result.exited_after_kill
    assert report.ok


def test_a_process_still_alive_after_kill_is_reported_honestly():
    """The failure that had no evidence.

    kill() used to be sent and the function returned immediately, so
    "killed successfully" and "still running" produced identical silence.
    A cleanup pass that cannot tell those apart cannot be debugged, which
    is why the first WebView2 orphan had to be reasoned about instead of
    read.
    """
    fake = _FakePsutil(survives_terminate=True, survives_kill=True)
    process_tree._psutil = lambda: fake  # noqa: SLF001
    try:
        identity = ProcessIdentity(pid=4296, create_time=1000.0, name="msedgewebview2.exe")
        report = process_tree.terminate_identities(
            [identity], terminate_grace_seconds=0.05, kill_grace_seconds=0.05
        )
    finally:
        process_tree._psutil = _REAL_PSUTIL_GETTER

    result = report.results[0]
    assert result.outcome == process_tree.STILL_ALIVE
    assert result.terminate_sent and result.kill_sent
    assert not result.exited_after_kill
    assert not report.ok, "a survivor must never be reported as a clean cleanup"
    assert report.survivors == [result]
    assert "still_alive=1" in report.summary()


def test_already_exited_descendants_are_harmless_and_named_as_such():
    process = _sleeper(0.01)
    identity = process_tree.identity_of(psutil.Process(process.pid)) if psutil.pid_exists(process.pid) else ProcessIdentity(pid=process.pid, create_time=1.0)
    process.wait(timeout=10)
    _wait_gone(process.pid)

    report = process_tree.terminate_identities([identity])

    assert report.ok
    assert report.results[0].outcome == process_tree.GONE
    assert not report.results[0].terminate_sent


def test_terminating_nothing_is_a_no_op():
    report = process_tree.terminate_identities([])
    assert report.results == [] and report.ok
    process_tree.terminate_pids([])  # must not raise


# ---------------------------------------------------------------------------
# PID reuse — the reason a bare number is not an identity
# ---------------------------------------------------------------------------

def test_a_recycled_pid_is_never_terminated():
    """Windows reuses PIDs freely, and this module holds its targets
    across a grace period. Acting on the number alone is how a cleanup
    pass kills a stranger while believing it only ever touches its own
    descendants.
    """
    bystander = _sleeper()
    try:
        # Same PID, a creation time that is not this process's: exactly
        # what a recycled PID looks like from a stale capture.
        stale = ProcessIdentity(pid=bystander.pid, create_time=1.0, name="msedgewebview2.exe")

        report = process_tree.terminate_identities([stale])

        assert report.results[0].outcome == process_tree.REUSED
        assert not report.results[0].terminate_sent
        assert bystander.poll() is None, "an unrelated process holding a recycled PID was killed"
    finally:
        _cleanup(bystander)


def test_an_identity_without_a_creation_time_is_left_alone():
    """Unverifiable is not the same as ours. Refusing to act is the safe
    reading, and it is reported rather than silently skipped."""
    bystander = _sleeper()
    try:
        report = process_tree.terminate_identities([ProcessIdentity(pid=bystander.pid)])

        assert report.results[0].outcome == process_tree.INACCESSIBLE
        assert report.unknown == report.results
        assert bystander.poll() is None
    finally:
        _cleanup(bystander)


def test_an_unrelated_process_with_the_same_image_name_is_never_touched():
    """The rule this module exists to keep. A name-based sweep would kill
    the msedge the user was browsing in; only a walk down from a process
    the launcher started may produce a target."""
    parent = _spawn_tree()
    bystander = _sleeper()   # same executable name, not our descendant
    try:
        captured = process_tree.capture_descendants(parent.pid)
        assert captured
        assert bystander.pid not in {identity.pid for identity in captured}

        process_tree.terminate_identities(captured)

        assert bystander.poll() is None, "a same-named process that was not ours was terminated"
    finally:
        _cleanup(parent, bystander)


# ---------------------------------------------------------------------------
# Late helpers
# ---------------------------------------------------------------------------

def test_a_helper_spawned_after_capture_is_still_cleaned_up():
    """WebView2 starts its renderer and GPU processes lazily.

    A snapshot taken when the window was asked to quit can be a process
    short by the time it exits. Expanding each captured identity to its
    own live descendants at cleanup time picks those up, and keeps the
    ownership rule intact transitively: a child of a proven descendant is
    a proven descendant.

    The tree here is the real shape — top → child → grandchild, with the
    grandchild appearing a second *after* the capture, so it can only be
    reached by expansion.
    """
    grandchild_code = "import time; time.sleep(30)"
    child_code = (
        "import subprocess, sys, time; time.sleep(1.0); "
        f"subprocess.Popen([sys.executable, '-c', {grandchild_code!r}]); time.sleep(30)"
    )
    top_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(30)"
    )
    top = subprocess.Popen([sys.executable, "-c", top_code])
    try:
        # Capture while only the child exists.
        captured = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            captured = process_tree.capture_descendants(top.pid)
            if captured:
                break
            time.sleep(0.05)
        assert len(captured) == 1, f"expected only the child at capture time, got {captured}"
        child_pid = captured[0].pid

        # Now wait for the grandchild that the capture could not have seen.
        late = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            late = process_tree.capture_descendants(child_pid)
            if late:
                break
            time.sleep(0.05)
        assert late, "the late helper never appeared, so this test proved nothing"
        late_pid = late[0].pid

        process_tree.terminate_identities(captured)

        assert _wait_gone(child_pid), "the captured child survived cleanup"
        assert _wait_gone(late_pid), "a helper spawned after the capture survived cleanup"
    finally:
        _cleanup(top)


# ---------------------------------------------------------------------------
# Robustness — cleanup may never break shutdown
# ---------------------------------------------------------------------------

def test_cleanup_survives_psutil_being_unavailable(monkeypatch):
    """psutil is an optional convenience here, not a dependency shutdown
    is allowed to require."""
    import builtins

    real_import = builtins.__import__

    def _no_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("no psutil in this build")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)

    assert process_tree.capture_descendants(1) == []
    report = process_tree.terminate_identities([ProcessIdentity(pid=1, create_time=1.0)])
    assert report.results[0].outcome == process_tree.INACCESSIBLE
    process_tree.terminate_pids([1])            # must not raise
    process_tree.terminate_descendants_of(1)    # must not raise


def test_a_psutil_that_raises_on_every_call_never_propagates():
    fake = _FakePsutil(raise_everything=True)
    process_tree._psutil = lambda: fake  # noqa: SLF001
    try:
        report = process_tree.terminate_identities(
            [ProcessIdentity(pid=4296, create_time=1000.0)],
            terminate_grace_seconds=0.05,
            kill_grace_seconds=0.05,
        )
    finally:
        process_tree._psutil = _REAL_PSUTIL_GETTER

    assert report.results, "a failure must still be reported, not swallowed into nothing"


def test_cleanup_is_bounded_even_when_nothing_ever_exits():
    """JARVIS itself must always be able to close. No path here may wait
    on a process indefinitely."""
    fake = _FakePsutil(survives_terminate=True, survives_kill=True)
    process_tree._psutil = lambda: fake  # noqa: SLF001
    started = time.monotonic()
    try:
        identities = [ProcessIdentity(pid=pid, create_time=1000.0) for pid in range(4300, 4310)]
        report = process_tree.terminate_identities(
            identities, terminate_grace_seconds=0.2, kill_grace_seconds=0.2
        )
    finally:
        process_tree._psutil = _REAL_PSUTIL_GETTER
    elapsed = time.monotonic() - started

    # One terminate grace plus one kill grace, whatever the processes do,
    # and never once per process.
    assert elapsed < 2.0, f"cleanup took {elapsed:.2f}s for ten stuck processes"
    assert len(report.survivors) == 10
    assert report.duration_seconds > 0


def test_repeated_cleanup_of_the_same_identities_is_idempotent():
    parent = _spawn_tree()
    captured = process_tree.capture_descendants(parent.pid)
    parent.kill()
    parent.wait(timeout=10)

    first = process_tree.terminate_identities(captured)
    second = process_tree.terminate_identities(captured)

    assert first.ok and second.ok
    assert second.results[0].outcome == process_tree.GONE
    assert not second.results[0].terminate_sent


# ---------------------------------------------------------------------------
# The report itself
# ---------------------------------------------------------------------------

def test_the_report_carries_nothing_that_needs_redacting():
    """Every field is a PID, an image name, a boolean or a duration.
    Diagnostics must never become a way for a path, a URL or a secret to
    reach a log file."""
    parent = _spawn_tree()
    try:
        report = process_tree.terminate_identities(process_tree.capture_descendants(parent.pid))
        payload = report.as_dict()

        assert set(payload) == {"duration_seconds", "outcomes", "processes"}
        for entry in payload["processes"]:
            assert set(entry["identity"]) == {"pid", "create_time", "name", "ppid", "source"}
            assert set(entry) == {
                "identity", "outcome", "alive_before", "terminate_sent",
                "exited_after_terminate", "kill_sent", "exited_after_kill",
                # Diagnostic fields. Every one is a short constant, an
                # exception *class* name or a bool — an exception's str()
                # is excluded on purpose, because a credential-store or
                # filesystem error can quote what it was looking for.
                "source", "terminate_error", "kill_error", "wait_error",
                "final_state", "final_checked",
            }
            # The guard this test exists for, applied to the new fields
            # too: nothing here may look like a filesystem location.
            for key in ("source", "terminate_error", "kill_error",
                        "wait_error", "final_state"):
                value = entry[key]
                assert isinstance(value, str), key
                assert "/" not in value and "\\" not in value and ":" not in value, key
    finally:
        _cleanup(parent)


def test_the_report_is_json_serialisable():
    """It ends up in a log line; a value that cannot be serialised is a
    diagnostic that fails exactly when it is needed."""
    import json

    report = process_tree.terminate_identities([ProcessIdentity(pid=0x7FFFFFFF, create_time=1.0)])
    json.dumps(report.as_dict())  # must not raise


def test_only_our_own_descendants_are_ever_targeted():
    """Never an enumeration and never a name match: the only input is a
    walk down from a process this launcher started.

    Kept as a source check *in addition to* the behavioural test above,
    because it catches the mechanism rather than one instance of it — a
    process_iter() added tomorrow fails here even if no test happens to
    exercise the path that calls it.

    Scans code only. The module's prose explains at length that it must
    never sweep up "an unrelated msedge the user was browsing in", and a
    naive substring search flags that sentence as the very thing it
    forbids — the same false-positive class that has bitten this
    project's packaging tests before.
    """
    for forbidden in ("process_iter", "taskkill", "pkill", "msedge", "chrome.exe"):
        assert forbidden not in _code_only(), f"process_tree reaches for {forbidden}"


class _FakePsutil:
    """A psutil that does what no real operating system will do on demand.

    Only used for the three situations a real process cannot be made to
    produce: surviving a kill, refusing to answer, and never exiting at
    all. Everything else in this file uses real processes.
    """

    class Error(Exception):
        pass

    class NoSuchProcess(Error):
        pass

    def __init__(self, survives_terminate=False, survives_kill=False, raise_everything=False,
                 exits_unobserved=False, wait_raises=False, create_time=1000.0):
        self._survives_terminate = survives_terminate
        self._survives_kill = survives_kill
        self._raise_everything = raise_everything
        # Models the hypothesis the Windows survivor investigation exists
        # to test: the process really does exit, but `wait_procs` never
        # observes it within the grace, so the pass reports `still_alive`
        # for something that has actually gone.
        self._exits_unobserved = exits_unobserved
        self._wait_raises = wait_raises
        self._create_time = create_time
        self._dead = set()
        self.waits = 0

    def Process(self, pid):  # noqa: N802 — mirrors psutil's own name
        if self._raise_everything:
            raise self.Error("psutil is unhappy")
        # A dead PID does not resolve, exactly as psutil's does not. This
        # is what lets the final re-resolve tell "gone" from "still here".
        if pid in self._dead:
            raise self.NoSuchProcess(pid)
        return _FakeProcess(self, pid)

    def wait_procs(self, processes, timeout=None):
        if self._raise_everything or self._wait_raises:
            raise self.Error("psutil is unhappy")
        self.waits += 1
        time.sleep(min(timeout or 0.0, 0.25))
        gone = [process for process in processes if process.pid in self._dead]
        alive = [process for process in processes if process.pid not in self._dead]
        if self._exits_unobserved and self.waits >= 2:
            # It dies *as the kill grace returns*: reported alive by this
            # call, absent to anything that looks afterwards. Gated on the
            # second wait because dying during the terminate grace would
            # simply be observed by the next one, which is the healthy
            # path and not the case under investigation.
            for process in alive:
                self._dead.add(process.pid)
        return gone, alive


class _FakeProcess:
    def __init__(self, owner, pid):
        self.pid = pid
        self._owner = owner

    def create_time(self):
        return self._owner._create_time

    def name(self):
        return "msedgewebview2.exe"

    def ppid(self):
        return 1

    def is_running(self):
        return self.pid not in self._owner._dead

    def children(self, recursive=False):
        return []

    def terminate(self):
        if not self._owner._survives_terminate:
            self._owner._dead.add(self.pid)

    def kill(self):
        if not self._owner._survives_kill:
            self._owner._dead.add(self.pid)


_REAL_PSUTIL_GETTER = process_tree._psutil


# ---------------------------------------------------------------------------
# Survivor diagnostics
#
# Temporary scaffolding for the Windows msedge.exe investigation on PR #17.
# `still_alive` proved only that wait_procs did not observe an exit within
# the shared deadline; it did not establish that the same PID *and*
# create_time were still present afterwards. These pin the difference.
# ---------------------------------------------------------------------------

def test_a_survivor_that_actually_exited_is_re_resolved_as_gone():
    """The reporting defect the final re-resolve exists to detect.

    A process that exits just as the kill grace expires is reported alive
    by `wait_procs` — that call computed its answer before the exit — and
    nothing afterwards looked again. `final_state` looks.
    """
    # `survives_kill` so the kill does not itself mark it gone: the point
    # is a process that outlives both signals *as far as the waits can
    # see*, and has nonetheless exited by the time anything looks again.
    fake = _FakePsutil(survives_terminate=True, survives_kill=True, exits_unobserved=True)
    process_tree._psutil = lambda: fake  # noqa: SLF001
    try:
        identity = ProcessIdentity(pid=4296, create_time=1000.0, name="msedge.exe")
        report = process_tree.terminate_identities(
            [identity], terminate_grace_seconds=0.05, kill_grace_seconds=0.05
        )
    finally:
        process_tree._psutil = _REAL_PSUTIL_GETTER

    result = report.results[0]
    assert result.final_checked is True
    assert result.final_state == process_tree.GONE
    # And the contract is deliberately unchanged: deciding what the
    # outcome *should* be needs the evidence this check collects.
    assert result.outcome == process_tree.STILL_ALIVE
    assert report.survivors == [result]
    assert report.ok is False


def test_a_survivor_that_is_genuinely_running_is_re_resolved_as_still_alive():
    fake = _FakePsutil(survives_terminate=True, survives_kill=True)
    process_tree._psutil = lambda: fake  # noqa: SLF001
    try:
        identity = ProcessIdentity(pid=4296, create_time=1000.0, name="msedge.exe")
        report = process_tree.terminate_identities(
            [identity], terminate_grace_seconds=0.05, kill_grace_seconds=0.05
        )
    finally:
        process_tree._psutil = _REAL_PSUTIL_GETTER

    result = report.results[0]
    assert result.final_checked is True
    assert result.final_state == process_tree.STILL_ALIVE
    assert result.terminate_sent and result.kill_sent


def test_a_survivor_whose_pid_was_recycled_is_re_resolved_as_reused():
    """The third answer. A PID that now belongs to something else is not
    a leak, and must never be reported as one — or terminated as one."""
    fake = _FakePsutil(survives_terminate=True, survives_kill=True)
    process_tree._psutil = lambda: fake  # noqa: SLF001
    try:
        identity = ProcessIdentity(pid=4296, create_time=1000.0, name="msedge.exe")
        report = process_tree.terminate_identities(
            [identity], terminate_grace_seconds=0.05, kill_grace_seconds=0.05
        )
        # The PID is now a different process: same number, new birthday.
        fake._create_time = 5000.0  # noqa: SLF001
        recheck = process_tree._final_state(fake, identity)  # noqa: SLF001
    finally:
        process_tree._psutil = _REAL_PSUTIL_GETTER

    assert report.results[0].final_state == process_tree.STILL_ALIVE
    assert recheck == process_tree.REUSED


def test_a_wait_that_raised_is_recorded_and_never_looks_like_no_exit():
    """`except Exception: return live` made a broken wait indistinguishable
    from a wait that completed and saw nothing exit."""
    fake = _FakePsutil(wait_raises=True)
    process_tree._psutil = lambda: fake  # noqa: SLF001
    try:
        identity = ProcessIdentity(pid=4296, create_time=1000.0, name="msedge.exe")
        report = process_tree.terminate_identities(
            [identity], terminate_grace_seconds=0.05, kill_grace_seconds=0.05
        )
    finally:
        process_tree._psutil = _REAL_PSUTIL_GETTER

    result = report.results[0]
    assert result.wait_error == "Error", result.wait_error
    assert result.final_checked is True


def test_the_report_says_whether_a_target_was_captured_or_expanded():
    """"One msedge.exe survived" does not say whether it was a process we
    captured up front or one we only noticed at cleanup time."""
    parent = _spawn_tree()
    try:
        captured = process_tree.capture_descendants(parent.pid)
        report = process_tree.terminate_identities(captured)
        sources = {result.source for result in report.results}
        assert sources, "every result must say where its target came from"
        assert sources <= {process_tree.FROM_CAPTURE, process_tree.FROM_EXPANSION,
                           process_tree.FROM_ROOT}
        assert process_tree.FROM_CAPTURE in sources
    finally:
        _cleanup(parent)


def test_terminate_and_kill_failures_are_recorded_separately():
    """Two different failures that used to produce the same silence."""
    class _Refuses(_FakeProcess):
        def terminate(self):
            raise _FakePsutil.Error("terminate refused")

        def kill(self):
            raise _FakePsutil.Error("kill refused")

    fake = _FakePsutil(survives_terminate=True, survives_kill=True)
    fake.Process = lambda pid: _Refuses(fake, pid)  # noqa: SLF001
    process_tree._psutil = lambda: fake  # noqa: SLF001
    try:
        identity = ProcessIdentity(pid=4296, create_time=1000.0, name="msedge.exe")
        report = process_tree.terminate_identities(
            [identity], terminate_grace_seconds=0.05, kill_grace_seconds=0.05
        )
    finally:
        process_tree._psutil = _REAL_PSUTIL_GETTER

    result = report.results[0]
    assert result.terminate_sent is False and result.terminate_error == "Error"
    assert result.kill_sent is False and result.kill_error == "Error"


def test_diagnostics_are_written_only_when_explicitly_asked_for(tmp_path, monkeypatch):
    """Opt-in. A cleanup pass runs on the shutdown path of a windowed build
    with no console; a recorder that wrote by default could fill a disk."""
    destination = tmp_path / "diag.jsonl"
    parent = _spawn_tree()
    try:
        monkeypatch.delenv(process_tree.DIAGNOSTICS_ENV, raising=False)
        process_tree.terminate_identities(process_tree.capture_descendants(parent.pid))
        assert not destination.exists(), "nothing may be written without the opt-in"
    finally:
        _cleanup(parent)

    parent = _spawn_tree()
    try:
        monkeypatch.setenv(process_tree.DIAGNOSTICS_ENV, str(destination))
        process_tree.terminate_identities(process_tree.capture_descendants(parent.pid))
    finally:
        _cleanup(parent)

    import json

    lines = [line for line in destination.read_text(encoding="utf-8").splitlines() if line]
    assert lines, "the opt-in must produce a record"
    record = json.loads(lines[-1])
    assert set(record) >= {"duration_seconds", "outcomes", "processes",
                           "recorded_at", "platform"}
    # The same redaction rule the rest of the module follows.
    blob = json.dumps(record)
    assert "\\\\" not in blob and "/home/" not in blob and "C:" not in blob


def test_a_diagnostics_destination_that_cannot_be_written_never_breaks_cleanup(monkeypatch):
    """A diagnostic must never be able to break the shutdown it explains."""
    parent = _spawn_tree()
    try:
        monkeypatch.setenv(process_tree.DIAGNOSTICS_ENV,
                           "/definitely/not/a/directory/diag.jsonl")
        report = process_tree.terminate_identities(
            process_tree.capture_descendants(parent.pid)
        )
        assert report.results, "cleanup still ran and still reported"
    finally:
        _cleanup(parent)
