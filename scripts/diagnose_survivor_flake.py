"""Capture Windows evidence for the `msedge.exe` survivor failure.

**Temporary scaffolding.** This exists to answer one question that the
current instrumentation cannot, and should be removed once it is answered.

The failure, on `windows-installer.yml` run 33251235787:

    test_no_browser_process_survives_any_fixture[alias.html]
    test_no_browser_process_survives_any_fixture[clean.html]

Each reported one `msedge.exe` as `still_alive` after the cleanup budget
(`app/coding/limits.py`: 3.0s terminate grace + 3.0s kill grace).

**What was not known.** `still_alive` was assigned when a target resolved,
and only ever *cleared* by a wait that saw the process exit. Nothing looked
again afterwards. So it proved that `psutil.wait_procs` did not observe an
exit within the shared deadline — not that the same PID *and* creation time
were still present. Those are different claims and they call for different
corrections, so this harness collects the evidence rather than guessing.

**How it works.** It runs the two failing tests exactly as CI does, in a
loop, with `JARVIS_PROCESS_DIAGNOSTICS` pointing at a JSONL file.
`process_tree.terminate_identities` appends one record per cleanup pass;
this script aggregates them and prints what each survivor turned out to be.

It deliberately drives *pytest*, not a reimplementation of the fixture. A
harness that set the browser up its own way would be measuring its own
setup — the failure is in the real path or it is nowhere.

**Bounded and safe.** Nothing here enumerates processes, matches on an
image name, or signals anything: it starts pytest and reads a file. The
cleanup it observes is the product's own, which only ever acts on
descendants of a process JARVIS started. An unrelated Edge window the user
is browsing in is never a target, here or anywhere else.

Usage:

    python scripts/diagnose_survivor_flake.py --iterations 12
    python scripts/diagnose_survivor_flake.py --iterations 20 --stop-on-capture
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

#: The two routes that failed. Named rather than derived so this harness
#: keeps investigating the reported failure even if the fixture set grows.
ROUTES = ("alias.html", "clean.html")

TEST_FILE = "tests/test_coding_browser_qa.py"
TEST_NAME = "test_no_browser_process_survives_any_fixture"

#: Per-iteration ceiling. Two browser tests plus a six-second cleanup
#: budget each; anything past this is a hang, not a slow run, and a
#: diagnostic that can block forever is not bounded.
ITERATION_TIMEOUT_SECONDS = 600


def _node_ids() -> list:
    return [f"{TEST_FILE}::{TEST_NAME}[{route}]" for route in ROUTES]


def _run_once(index: int, diagnostics: Path, verbose: bool) -> dict:
    """One pytest invocation. Returns what happened, never raises."""
    before = diagnostics.stat().st_size if diagnostics.exists() else 0

    environment = dict(os.environ)
    # The suite the way the installer build runs it, plus the recorder.
    environment["JARVIS_LOG_LEVEL"] = "WARNING"
    environment.setdefault("JARVIS_DB_PATH", str(Path(tempfile.gettempdir()) / "jarvis_survivor.db"))
    environment["JARVIS_PROCESS_DIAGNOSTICS"] = str(diagnostics)

    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 — argv list, shell=False
            [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", *_node_ids()],
            cwd=str(REPO_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            timeout=ITERATION_TIMEOUT_SECONDS,
            shell=False,
        )
        code, output = completed.returncode, completed.stdout + completed.stderr
    except subprocess.TimeoutExpired:
        code, output = -1, f"timed out after {ITERATION_TIMEOUT_SECONDS}s"

    elapsed = time.monotonic() - started
    tail = [line for line in output.splitlines() if line.strip()][-4:]
    if verbose:
        print("\n".join(f"      {line}" for line in tail))

    return {
        "index": index,
        "exit_code": code,
        "failed": code != 0,
        "seconds": round(elapsed, 1),
        "tail": tail,
        "new_bytes": (diagnostics.stat().st_size if diagnostics.exists() else 0) - before,
    }


def _records(diagnostics: Path) -> list:
    if not diagnostics.exists():
        return []
    out = []
    for line in diagnostics.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _survivors(records: list) -> list:
    found = []
    for record in records:
        for entry in record.get("processes", []):
            if entry.get("outcome") == "still_alive":
                found.append(entry)
    return found


def _describe(entry: dict) -> str:
    identity = entry.get("identity", {})
    return (
        f"pid={identity.get('pid')} "
        f"create_time={identity.get('create_time')} "
        f"name={identity.get('name') or 'unknown'} "
        f"ppid={identity.get('ppid')} "
        f"source={entry.get('source') or 'unknown'} "
        f"terminate_sent={entry.get('terminate_sent')} "
        f"terminate_error={entry.get('terminate_error') or 'none'} "
        f"kill_sent={entry.get('kill_sent')} "
        f"kill_error={entry.get('kill_error') or 'none'} "
        f"wait_error={entry.get('wait_error') or 'none'} "
        f"final_checked={entry.get('final_checked')} "
        f"final_state={entry.get('final_state') or 'not_checked'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--stop-on-capture", action="store_true",
                        help="stop as soon as one iteration fails")
    parser.add_argument("--diagnostics", default="",
                        help="where to write the JSONL (default: a temp file)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    diagnostics = Path(args.diagnostics) if args.diagnostics else (
        Path(tempfile.gettempdir()) / "jarvis_survivor_diagnostics.jsonl"
    )
    diagnostics.parent.mkdir(parents=True, exist_ok=True)
    if diagnostics.exists():
        diagnostics.unlink()

    print(f"platform     : {sys.platform} (os.name={os.name})")
    print(f"routes       : {', '.join(ROUTES)}")
    print(f"iterations   : {args.iterations}")
    print(f"diagnostics  : {diagnostics}")
    print()

    runs = []
    for index in range(1, args.iterations + 1):
        result = _run_once(index, diagnostics, args.verbose)
        runs.append(result)
        verdict = "FAILED" if result["failed"] else "passed"
        print(f"  iteration {index:02d}  {verdict}  {result['seconds']}s")
        if result["failed"] and args.stop_on_capture:
            print("  stopping: the failure was captured")
            break

    records = _records(diagnostics)
    survivors = _survivors(records)
    failures = [run for run in runs if run["failed"]]

    print()
    print("=" * 70)
    print(f"iterations run     : {len(runs)}")
    print(f"iterations failed  : {len(failures)}")
    print(f"cleanup passes     : {len(records)}")
    print(f"survivors recorded : {len(survivors)}")

    if survivors:
        print()
        print("--- every survivor, as recorded ---")
        for entry in survivors:
            print(f"  {_describe(entry)}")
        print()
        print("--- final re-resolve, counted ---")
        for state, count in sorted(Counter(
            entry.get("final_state") or "not_checked" for entry in survivors
        ).items()):
            print(f"  {state:16} {count}")
        print()
        print("--- capture source, counted ---")
        for source, count in sorted(Counter(
            entry.get("source") or "unknown" for entry in survivors
        ).items()):
            print(f"  {source:16} {count}")
        wait_errors = Counter(
            entry.get("wait_error") for entry in survivors if entry.get("wait_error")
        )
        print()
        print(f"--- wait_procs exceptions: {sum(wait_errors.values())} ---")
        for name, count in sorted(wait_errors.items()):
            print(f"  {name:16} {count}")
    else:
        print()
        print("No survivor was recorded. The failure was NOT reproduced in this run,")
        print("which is not the same as the cause being ruled out.")

    print("=" * 70)
    # Zero means the harness itself ran, not that the product is healthy —
    # the evidence above is the output. A non-zero exit would make this
    # look like a gate, and it is not one.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
