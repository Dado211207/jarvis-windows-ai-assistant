# Two bounded investigations, and where each one stopped

Both were given at most two targeted diagnostic runs. Both reached that
limit. **Neither produced a demonstrated cause, and neither has a fix.**
This is the record of what was measured, so the next attempt does not
start from the same wrong place.

---

## #144 — the `msedge.exe` survivor

### The failure, three times

| Where | Signature |
|---|---|
| `fdef269`, acceptance job `101218076939` | `10 leftover process(es) in 6.05s`, `msedge.exe` pid 1664, `final_state=still_alive` |
| post-merge CI Windows smoke, job `101317543827` | `msedge.exe` pid 1320, `terminate_sent=True kill_sent=True`, all error fields empty, `final_state=still_alive` |
| post-merge installer acceptance, job `101317560899` | Step 20: `OK: every owned process tree reported terminated`, then `FAILED: Processes survived — msedge.exe (pid 4040) — a JARVIS browser profile` |

The third is a **different manifestation** from the first two: cleanup
reported everything it owned as terminated, and a profile-based sweep
still found a survivor. Whatever that process was, it was not in the
captured target set.

### Hypothesis H1′, and how it died

`process_tree._settle()` marks a process gone only when
`psutil.wait_procs()` says so. `wait_procs` calls `Process.wait()` and
swallows `TimeoutExpired` — which is why `wait_error` is empty in every
observed failure. psutil 7.2.2's Windows `wait()` calls
`WaitForSingleObject` and then, despite its own comment reading
"WAIT_OBJECT_0, meaning the process is gone", polls `pid_exists()` until
it clears or the timeout expires.

`pid_exists` reaches `psutil_check_phandle`, which for an exit code other
than `STILL_ACTIVE` reports the process **running** whenever
`psutil_pid_in_pids(pid) == 1` — an `EnumProcesses` scan.

So the hypothesis required a terminated process to stay enumerated.
**Run 1 (`33971668902`) measured that directly and did not find it**:
8/8 controls valid; both the handle-held and handle-closed arms cleared
`pid_exists` and `EnumProcesses` membership below the harness's ~10 ms
first-sample resolution, in 8 of 8 iterations each. Holding the handle
open made no measurable difference.

**"0.0 s" means "already clear when first observed", not literally
zero**, and this falsifies the lag explanation *for the conditions
tested*. It does not prove every historical survivor was genuinely
running, and it does not eliminate every observation failure.

### What run 2 tried, and what it found

Run 2 (`33973016327`, job `101324761289`) instrumented the real route:
all 15 parametrised browser-QA fixtures, 6 iterations, kernel-level
probes on any survivor.

    iterations run     : 6
    iterations failed  : 0
    cleanup passes     : 96
    survivors recorded : 0

**Not reproduced.** Which is not the same as ruled out.

A third run (`33973159675`) fired accidentally from a push and is
excluded from this conclusion; using it would have been the
rerun-until-it-reproduces pattern the investigation was told to avoid.

### A correction worth keeping

`kill_error=''` does **not** establish that `TerminateProcess`
succeeded. Verbatim from psutil 7.2.2 `arch/windows/proc.c`:

```c
if (!TerminateProcess(hProcess, SIGTERM)) {
    // ERROR_ACCESS_DENIED may happen if the process already died.
    if (GetLastError() != ERROR_ACCESS_DENIED) {
        psutil_oserror_wsyscall("TerminateProcess");
        return NULL;
    }
}
```

`ERROR_ACCESS_DENIED` is suppressed and the call returns success. Any
future reading of these records must not treat an empty `kill_error` as
proof the native call worked.

### The instrumentation, and its limit

`app/launcher/process_probe.py` asks Windows directly about a survivor —
handle opened for query and synchronise, creation time read from the
handle rather than re-resolved by PID, `WaitForSingleObject(handle, 0)`,
`GetExitCodeProcess`, and whether a `PROCESS_TERMINATE` handle is even
grantable. Signalled, timed out, inaccessible and probe-failure stay four
distinct outcomes. It never signals anything.

**It only records when `JARVIS_PROCESS_DIAGNOSTICS` is set.** Ordinary CI
does not set it, so it would *not* have fired during either post-merge
failure, and it currently lives on `claude/diagnose-pid-visibility`
rather than on `main`. Anyone hoping the next natural occurrence will
carry kernel evidence has to arrange that first.

### Where a third attempt should start

Not with the retained-handle idea; that has been measured. The installer
Step 20 observation is the untouched thread — a survivor **outside** the
captured set, which `process_tree` is by design incapable of reporting,
because it only ever acts on descendants of a process JARVIS started.
Whether Edge's broker model re-parents children away from that tree is
unexamined, and a profile-scan match does not by itself establish it.

---

## #145 — the interpreter abort after the totals

A pytest run prints its summary and then dies:

    3274 passed, 7 skipped, 185 deselected
    terminate called without an active exception   (exit 134)

**Test totals followed by an interpreter abort are a failed run.**

### Hypothesis, still unproven

`app/voice/kokoro/engine.py` holds its `InferenceSession` on a
module-level singleton with no `close()`, no `__del__` and no `atexit`,
so it survives to interpreter finalisation and is destroyed in an order
Python does not guarantee — possibly after onnxruntime's own static
teardown, where a throwing C++ destructor becomes `std::terminate()`.
onnxruntime logs immediately before the abort:

    [E:onnxruntime:, sequential_executor.cc:671 ExecuteKernel] Non-zero
    status code returned while running ReduceMean node.
    Name:'/decoder/decoder/generator/resblocks.3/adain1.0/ReduceMean_2'
    Status Message: GetElementType is not implemented

### The two runs

**Run 1 — synthetic workload, 12 iterations per arm, fresh subprocess
each.** Real inference ran (~11 s per iteration).

    baseline: n=12 aborts=0 exit_codes={0: 12}
    unload  : n=12 aborts=0 exit_codes={0: 12}

Inconclusive by construction: an arm that never fails cannot show
whether releasing the session prevents a failure. The synthetic workload
was simply the wrong probe.

**Run 2 — the real suite combination observed to abort, 1 iteration per
arm.**

    baseline [0] ABORT exit=-6 terminate_msg=True
    unload   [0] ok    exit=0  terminate_msg=False

### Why this is not a fix

`n=1` per arm, against a defect measured as intermittent at roughly one
run in five. A clean release arm at that sample size is well within
chance. **No production change was made**; `engine.py` is untouched.

A run at `n>=10` per arm would settle it. That needs approval, because
both runs for this issue are spent.

### Two harness defects caught before any number was believed

The first worker kept the generator returned by `synthesise()` without
draining it, so **no inference ran at all** — the model was never loaded
— and it would have reported a clean teardown from a workload that did
nothing. The same script also counted a worker exiting `1` as `ok`,
which is how a broken harness reports "no aborts". Both are fixed, and
the run now declares itself invalid rather than publishing a number.

---

## Scope

No production behaviour changed in either investigation. No assertion was
edited, no timeout raised, nothing skipped, and no extra signal was sent
to any process to collect evidence. The `native_probes` record lives in
the opt-in diagnostics JSONL rather than on `CleanupResult`, so the
cleanup report's shape — and the test that pins its exact key set — is
untouched.
