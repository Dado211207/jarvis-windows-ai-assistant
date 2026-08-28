# The WebView2 process that outlived JARVIS

A post-mortem of the failure that blocked the v0.2.0-rc1 installer, and
of the three defects that turned out to be behind it.

---

## What happened

Two **Windows Installer** runs fired on the same commit
(`091acd25db64938e7cbd3fc6f14f57252b2617a5`) at the same moment — one
from `workflow_dispatch`, one from the `pull_request` event. They built
the same installer on two separate hosted runners.

One passed all ten lifecycle cycles. The other failed on the second:

```
=== Phase D.1: cold start and graceful quit (1 of 10) ===
OK: cycle 1 — ready in 4.7s (session 7c526fcbd57984e8), exited cleanly,
port released, no JARVIS or WebView2 process left behind

=== Phase D.2: cold start and graceful quit (2 of 10) ===
FAILED: Cycle 2: 1 WebView2 process(es) started by JARVIS outlived it
(pids [4296]).
```

Run `31900370743`, job `95050088943`, runner `1000001049`.

Same code, same artifact, opposite results. That is the definition of a
flaky check — and a flaky check on shutdown is not something to re-run
until it goes green, because the thing it is flaking on is whether the
product leaves processes running on a real person's computer.

## What the evidence could and could not say

The uploaded artifact contained the installer log and two self-test logs.
It did not contain the window child's log, so the one file that would
have named the cause did not exist to read. **That is itself the first
finding**, and it is fixed: `scripts/test_clean_install.py` now copies
`jarvis.log`, `jarvis-window.log` and `boot_trace.log` into the artifact
on both the failure and the success path.

Reading the code rather than the logs, three defects in
`app/launcher/process_tree.py` each independently explain the failure.
Rather than guess which one produced PID 4296, all three are fixed.

---

## Defect 1 — nothing waited after `kill()`

```python
_, alive = psutil.wait_procs(survivors, timeout=TERMINATE_GRACE_SECONDS)
for process in alive:
    try:
        process.kill()
    except Exception:
        continue
# ...and the function returned here.
```

`terminate()` was sent, a three-second grace was observed, `kill()` was
sent — and the function returned immediately. Whether the process
actually died was never established by anyone.

The process this matters for is precisely the one that had already
ignored `TerminateProcess` for three full seconds. It is the one that
needs a moment more, and it was the one nobody was watching.

`gui.quit()` calls `window.stop()` synchronously, so the tray parent then
proceeded to shut down the server, release the port and exit — while a
WebView2 process that had been sent a kill was still winding down. The
acceptance test, checking immediately afterwards, saw it.

**Fixed:** a `KILL_GRACE_SECONDS` wait after `kill()`, symmetric with the
terminate grace. A process that needs escalating has already shown it is
slow to go; giving `kill` *less* time than `terminate` was the wrong way
round.

## Defect 2 — nothing reported what happened

"Killed successfully" and "still running" produced identical silence.
`terminate_pids()` returned `None` and logged nothing, so a cleanup pass
could not be debugged after the fact — which is why defect 1 had to be
reasoned about from source rather than read from a log.

**Fixed:** `terminate_identities()` returns a `CleanupReport` with a
`CleanupResult` per process, recording whether it was alive beforehand,
whether terminate was sent, whether it exited during the grace, whether
kill was sent, whether it exited after that, and how long the whole pass
took. Six outcomes are distinguished: `already_gone`, `terminated`,
`killed`, `still_alive`, `inaccessible`, `pid_reused`. A survivor is
logged as a warning naming the process.

Every field is a PID, an image name, a boolean or a duration. There is
deliberately no executable path: on Windows a full path contains the
account name, and this record goes into a log file.

## Defect 3 — bare PIDs, terminated seconds after capture

This is the one that was not merely a diagnostic weakness.

Descendants were captured as integers, held across a three-second grace
period, and then passed to `psutil.Process(pid).terminate()`. Windows
recycles PIDs aggressively. A PID captured before the grace can belong to
something else entirely by the time cleanup acts on it — so the module
could terminate a stranger's process while believing its own first rule,
that only its own descendants are ever touched.

**Fixed:** `ProcessIdentity` — PID plus creation time, plus image name
and parent PID for diagnostics. Two processes can share a PID; they
cannot share a PID *and* a creation timestamp. `_resolve()` re-checks the
creation time before touching anything and reports `pid_reused` rather
than acting. An identity captured without a creation time is
`inaccessible` and is deliberately left alone: unverifiable is not the
same as ours.

---

## Two further gaps found while fixing those

**The last capture came after the last poll.** The graceful loop checked
`poll()` first and captured descendants second, so a WebView2 helper born
in the final sleep interval before the window child exited was never
recorded — and a process that is never captured is never cleaned up.
WebView2 starts its renderer and GPU processes lazily, so "a helper
appears just before shutdown completes" is ordinary, not exotic. The
order is now capture-then-poll.

**A helper could still appear after the final capture.** Cleanup now
expands each captured identity to its own live descendants at the moment
it acts. Anything picked up that way is a live child of a process already
proven to be our descendant, so the ownership rule holds transitively.

---

## What the acceptance test was doing wrong

```python
orphaned_webviews = [pid for pid in webview_before if psutil.pid_exists(pid)]
```

Two problems in one line. `pid_exists()` asks whether *some* process
holds that number, which a recycled PID answers wrongly — a false
failure. And it asked exactly once, immediately, with no allowance for
the operating system finishing a teardown JARVIS had already ordered.

The replacement waits for the exact captured identities to reach a
terminal state, within a bound of `WEBVIEW_SETTLE_TIMEOUT_SECONDS`
(10 s), and names every survivor with its PID, image name, parent and
status.

**That wait cannot mask a leak, which is the point.** By the time the
test looks, JARVIS's own cleanup has already run to completion —
`quit()` calls `window.stop()` synchronously, and `stop()` does not
return until terminate, its grace, kill and *its* grace have all
happened. A genuinely leaked process is not mid-teardown; it is running,
and it is still running when the deadline expires. What the wait absorbs
is the last moments of a process already killed, on a loaded runner.

The test also now captures WebView2 children **twice** — at ready-time
and again immediately before the parent is killed — because a helper the
test never recorded is a leak it could never report.

The ten-cycle criterion is unchanged, no assertion was weakened, no
failure became a skip, and `PROCESS_EXIT_TIMEOUT_SECONDS` was not
touched.

---

## A note on zombies

`_is_zombie()` exists because these tests run on Linux, where a process
killed while its parent is still alive lingers as a zombie until the
parent reaps it — visible to `pid_exists()` and not reported as gone by
`wait_procs()`, despite being dead and holding nothing. Counting one as a
survivor would fail the product for something it did correctly.

Windows, where this ships, has no zombies at all. The check is always
`False` there. It is here so that the machines the tests run on agree
with reality.

---

## What this does not prove

The fix is verified by the ten-cycle test on GitHub's Windows runners,
run twice consecutively on the same commit — because a single green run
after a failure is not evidence about an intermittent path.

Not verified: the same shutdown on a machine with a different WebView2
version, a different GPU, or antivirus software inspecting process
creation. The failure mode this fixes is timing-sensitive by nature, and
a CI runner is one machine shape among many.
