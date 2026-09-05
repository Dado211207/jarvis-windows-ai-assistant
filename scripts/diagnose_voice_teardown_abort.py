"""Measure the `terminate called without an active exception` teardown abort.

**Temporary scaffolding**, for the voice-stack investigation. Remove it
once the question is answered.

The observation
---------------
A pytest run reports its totals and then dies at interpreter shutdown::

    3274 passed, 7 skipped, 185 deselected
    terminate called without an active exception
    (exit 134)

Running the voice suites directly printed a cause immediately before it::

    [E:onnxruntime:, sequential_executor.cc:671 ExecuteKernel] Non-zero
    status code returned while running ReduceMean node.
    Name:'/decoder/decoder/generator/resblocks.3/adain1.0/ReduceMean_2'
    Status Message: GetElementType is not implemented

**Test totals followed by an interpreter abort are a failed run**, not a
passing one with noise after it, which is why this is being measured
rather than filtered out.

The hypothesis, and what would falsify it
-----------------------------------------
`app/voice/kokoro/engine.py` holds its `InferenceSession` on a
module-level singleton (`engine`) with no `close()`, no `__del__` and no
`atexit`. It therefore survives until interpreter finalisation, where it
is destroyed in an order Python does not guarantee — possibly after
onnxruntime's own static teardown. A C++ destructor that throws with no
active exception becomes `std::terminate()`.

That story is **unproven**. It predicts one thing that can be measured:
releasing the session deterministically, after inference has finished and
before interpreter shutdown, should remove the abort. If the abort rate
is unchanged, the explanation is wrong.

Method
------
Two arms, identical workloads, each iteration in a **fresh subprocess**
so that no state carries between them:

  baseline — synthesise, then exit, leaving the singleton to finalisation
  unload   — synthesise, wait for every call to return, then
             `engine.unload()`, then exit

The subprocess's **actual exit code** is recorded, not a guess from its
output: on POSIX an abort is -6 (SIGABRT), reported by shells as 134.

Releasing the session while an inference is still running would be a
different experiment and a misleading one, so the workload is fully
synchronous and every call has returned before `unload()` is reached.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Short, ordinary text. The defect is claimed to be about teardown, not
#: about any particular utterance, so the workload stays small and the
#: iteration count does the work.
PHRASES = (
    "Good evening.",
    "The preferences file is written atomically.",
)

WORKER_TIMEOUT_SECONDS = 180

SIGABRT_EXIT_CODES = {-6, 134}


def _worker(arm: str) -> int:
    """One workload, in this process. Returns an exit code."""
    sys.path.insert(0, str(REPO_ROOT))
    from app.voice.kokoro import engine as kokoro_engine

    reason = kokoro_engine.engine.unavailable_reason()
    if reason:
        print(f"SKIP: engine unavailable: {reason}")
        return 0

    total = 0
    for phrase in PHRASES:
        # `synthesise` yields per sentence, so it must be **drained** for
        # any inference to happen at all. The first version of this script
        # kept the generator and measured nothing: the session was never
        # even loaded. Draining it here is also what makes the release in
        # the other arm safe — every inference has returned before
        # `unload()` is reached, rather than racing it.
        chunks = 0
        for chunk in kokoro_engine.engine.synthesise(
            phrase, kokoro_engine.assets.DEFAULT_VOICE_KEY
        ):
            samples = getattr(chunk, "samples", chunk)
            total += int(getattr(samples, "size", 0) or 0)
            chunks += 1
        print(f"  synthesised {chunks} chunk(s) for {phrase!r}")

    if total <= 0:
        # Nothing was computed, so there is no teardown to measure and no
        # conclusion to draw. Say so rather than exiting 0 and being
        # counted as a clean run.
        print("SKIP: no audio was produced; the workload did not exercise the session.")
        return 0

    if arm == "unload":
        kokoro_engine.engine.unload()
        print("  released the session before interpreter shutdown")
    return 0


def _run_iteration(arm: str, index: int) -> dict:
    """One fresh subprocess. Returns what happened; never raises."""
    environment = dict(os.environ)
    environment["JARVIS_LOG_LEVEL"] = "WARNING"
    environment.setdefault(
        "JARVIS_DB_PATH", str(Path(tempfile.gettempdir()) / "jarvis_voice_teardown.db")
    )

    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 — argv list, shell=False
            [sys.executable, str(Path(__file__).resolve()), "--worker", arm],
            cwd=str(REPO_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            timeout=WORKER_TIMEOUT_SECONDS,
            shell=False,
        )
        code = completed.returncode
        output = completed.stdout + completed.stderr
    except subprocess.TimeoutExpired:
        code, output = None, f"timed out after {WORKER_TIMEOUT_SECONDS}s"

    aborted = code in SIGABRT_EXIT_CODES
    # Anything non-zero that is not the abort under study is a broken
    # worker. Counting it as "ok" would let a harness bug read as a clean
    # teardown, which is how the first version of this script reported
    # "no aborts" while never loading the model at all.
    broken = (code not in (0, None)) and not aborted
    onnx_line = any("onnxruntime" in line for line in output.splitlines())
    terminate_line = "terminate called without an active exception" in output
    return {
        "arm": arm,
        "index": index,
        "exit_code": code,
        "aborted": aborted,
        "terminate_message": terminate_line,
        "onnxruntime_error_logged": onnx_line,
        "seconds": round(time.monotonic() - started, 2),
        "skipped": "SKIP:" in output,
        "broken": broken,
        "tail": [line for line in output.splitlines() if line.strip()][-3:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", default="", help=argparse.SUPPRESS)
    parser.add_argument("--iterations", type=int, default=12)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    if args.worker:
        return _worker(args.worker)

    print(f"python      : {sys.version.split()[0]} on {sys.platform}")
    try:
        import onnxruntime

        print(f"onnxruntime : {onnxruntime.__version__}")
    except Exception as exc:  # noqa: BLE001
        print(f"onnxruntime : unavailable ({exc.__class__.__name__})")
        print("NOT RUN: nothing to measure without onnxruntime.")
        return 0
    print(f"iterations  : {args.iterations} per arm, each in a fresh subprocess")
    print()

    records = []
    for arm in ("baseline", "unload"):
        for index in range(args.iterations):
            record = _run_iteration(arm, index)
            records.append(record)
            flag = "ABORT" if record["aborted"] else ("BROKEN" if record["broken"] else "ok    ")
            print(
                f"  {arm:<8} [{index:>2}] {flag} exit={record['exit_code']} "
                f"terminate_msg={record['terminate_message']} "
                f"{record['seconds']}s"
            )

    print()
    broken = [r for r in records if r["broken"]]
    if broken:
        print(f"HARNESS INVALID: {len(broken)}/{len(records)} iterations exited non-zero "
              "for a reason that is not the abort under study. No conclusion is drawn.")
        for record in broken[:3]:
            for line in record["tail"]:
                print(f"    {line}")
        return 0

    skipped = [r for r in records if r["skipped"]]
    if skipped:
        print(f"NOT RUN: {len(skipped)}/{len(records)} iterations skipped — the engine "
              "reported itself unavailable, so no conclusion is drawn.")
        return 0

    for arm in ("baseline", "unload"):
        arm_records = [r for r in records if r["arm"] == arm]
        aborts = sum(1 for r in arm_records if r["aborted"])
        codes = Counter(r["exit_code"] for r in arm_records)
        print(f"{arm:<8}: n={len(arm_records)} aborts={aborts} exit_codes={dict(codes)}")

    base = sum(1 for r in records if r["arm"] == "baseline" and r["aborted"])
    rel = sum(1 for r in records if r["arm"] == "unload" and r["aborted"])
    print()
    if base == 0:
        print("INCONCLUSIVE: the baseline arm did not abort at all, so this run "
              "cannot say whether releasing the session changes anything.")
    elif rel == 0:
        print(f"Releasing removed every abort in this run ({base} -> 0). Supports the "
              "finalisation-order explanation; does not prove it alone.")
    elif rel < base:
        print(f"Releasing reduced but did not remove aborts ({base} -> {rel}). "
              "Partial at best; not a fix.")
    else:
        print(f"Releasing did not help ({base} -> {rel}). The finalisation-order "
              "explanation is wrong, or incomplete.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        print(f"Wrote {len(records)} records to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
