"""The command runner: what the child sees, and whether it really dies.

Two failures this guards against, both of which look fine from the
outside:

* A child process inherits the parent's environment and therefore the
  user's Anthropic API key. `npm install` running a postinstall script
  would have it.
* "Killed" means `kill()` did not raise, rather than the process being
  gone. A dev server that spawned a child outlives the task, holds the
  port, and the next preview mysteriously fails.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from app.coding import limits, runner
from app.coding.runner import CommandHandle, build_environment, redacted_environment_summary
from tests import coding_fixtures as fx


def alive(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def wait_gone(pids, timeout: float = 15.0) -> list:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = [p for p in pids if alive(p)]
        if not remaining:
            return []
        time.sleep(0.1)
    return [p for p in pids if alive(p)]


# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------

def test_the_child_never_sees_the_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", fx.FAKE_ANTHROPIC_KEY)
    env = build_environment()
    assert "ANTHROPIC_API_KEY" not in env
    assert fx.FAKE_ANTHROPIC_KEY not in "".join(env.values())


@pytest.mark.parametrize("name", [
    "ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN", "NPM_TOKEN", "OPENAI_API_KEY", "JARVIS_SESSION_TOKEN",
])
def test_credential_variables_are_not_passed_through(monkeypatch, name):
    monkeypatch.setenv(name, "a-value-that-must-not-travel")
    env = build_environment()
    assert name not in env, f"{name} reached the child environment"


def test_the_environment_is_an_allowlist_not_a_denylist(monkeypatch):
    """A denylist misses the next variable somebody invents. Something
    invented right now must not be present."""
    monkeypatch.setenv("SOME_BRAND_NEW_SECRET_2026", "value")
    assert "SOME_BRAND_NEW_SECRET_2026" not in build_environment()


def test_the_child_still_gets_what_a_build_actually_needs(monkeypatch):
    env = build_environment()
    assert env.get("PATH"), "a child with no PATH cannot find node or python"
    # A build that cannot find a home directory fails in confusing ways.
    assert any(k in env for k in ("HOME", "USERPROFILE", "SystemRoot", "TEMP", "TMP"))


def test_extra_values_can_be_added_explicitly():
    env = build_environment({"PORT": "5199"})
    assert env["PORT"] == "5199"


def test_the_environment_summary_shows_names_and_never_values(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    summary = redacted_environment_summary(build_environment({"SECRET_ISH": "swordfish"}))
    assert "swordfish" not in repr(summary)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def test_a_simple_command_runs_and_reports_its_exit_code(tmp_path):
    outcome = runner.run([sys.executable, "-c", "print('hello')"], tmp_path, "project")
    assert outcome.exit_code == 0
    assert "hello" in outcome.stdout
    assert outcome.ok is True
    assert outcome.timed_out is False


def test_a_failing_command_reports_the_failure_not_an_exception(tmp_path):
    outcome = runner.run([sys.executable, "-c", "import sys; sys.exit(3)"], tmp_path, "project")
    assert outcome.exit_code == 3
    assert outcome.ok is False


def test_a_missing_program_is_reported_not_raised(tmp_path):
    outcome = runner.run(["definitely-not-a-real-program-xyz"], tmp_path, "project")
    assert outcome.ok is False
    assert outcome.exit_code != 0


def test_output_is_capped_and_says_so(tmp_path):
    """A build that prints a hundred megabytes must not become a hundred
    megabytes of memory, and the truncation must be visible."""
    outcome = runner.run(
        [sys.executable, "-c",
         "print('x' * 200)\n" * 1 + "import sys\nfor _ in range(20000): print('y' * 200)"],
        tmp_path, "project", timeout_seconds=60,
    )
    assert outcome.truncated is True
    assert len(outcome.stdout.encode()) <= limits.MAX_COMMAND_OUTPUT_BYTES * 2
    assert "truncat" in outcome.stdout.lower() or "truncat" in outcome.stderr.lower()


def test_one_enormous_line_still_keeps_what_fits(tmp_path):
    """An early version discarded any line that did not fit whole, so a
    single-line minified bundle produced 63 bytes of output."""
    outcome = runner.run(
        [sys.executable, "-c", f"print('z' * {limits.MAX_COMMAND_OUTPUT_BYTES * 2})"],
        tmp_path, "project", timeout_seconds=60,
    )
    assert len(outcome.stdout) > 1000, "an oversized single line was discarded whole"


def test_a_timeout_ends_the_process_and_reports_it(tmp_path):
    started = time.monotonic()
    outcome = runner.run(
        [sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"],
        tmp_path, "project", timeout_seconds=2.0,
    )
    elapsed = time.monotonic() - started
    assert outcome.timed_out is True
    assert outcome.ok is False
    assert elapsed < 30, "the timeout must be bounded, not merely eventual"


# ---------------------------------------------------------------------------
# Ownership — the part that actually matters
# ---------------------------------------------------------------------------

def test_a_timed_out_command_leaves_no_process_behind(tmp_path):
    argv = fx.process_spawner(tmp_path)
    handle = CommandHandle(argv, tmp_path, "project")
    outcome = runner.run(argv, tmp_path, "project", timeout_seconds=3.0, handle=handle)
    assert outcome.timed_out is True

    parent = handle.pid
    child_pids = [
        int(line.split()[1]) for line in outcome.stdout.splitlines()
        if line.startswith("child ")
    ]
    survivors = wait_gone([parent] + child_pids)
    assert survivors == [], f"processes survived the timeout: {survivors}"


def test_stopping_a_command_ends_its_whole_tree(tmp_path):
    argv = fx.process_spawner(tmp_path)
    handle = CommandHandle(argv, tmp_path, "project")
    handle.start(build_environment())

    # Let the child actually exist before asking for the tree to die.
    deadline = time.monotonic() + 10
    children = []
    while time.monotonic() < deadline and not children:
        try:
            children = psutil.Process(handle.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            break
        time.sleep(0.1)
    assert children, "the fixture must actually spawn a child, or this proves nothing"

    pids = [handle.pid] + [c.pid for c in children]
    report = handle.stop("test")

    survivors = wait_gone(pids)
    assert survivors == [], f"processes survived stop(): {survivors}"
    assert isinstance(report, dict)


def test_the_cleanup_report_is_structured_and_never_raises(tmp_path):
    handle = CommandHandle([sys.executable, "-c", "pass"], tmp_path, "project")
    handle.start(build_environment())
    time.sleep(0.5)
    report = handle.stop("already finished")
    assert isinstance(report, dict)
    # Stopping twice must also not raise.
    assert isinstance(handle.stop("again"), dict)


def test_the_ledger_stops_everything_it_tracks(tmp_path):
    handles = []
    for _ in range(2):
        handle = CommandHandle(
            [sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"],
            tmp_path, "project")
        handle.start(build_environment())
        runner.ledger.track(handle)
        handles.append(handle)

    pids = [h.pid for h in handles]
    assert runner.ledger.live_count() >= 2

    runner.ledger.stop_all("test cleanup")
    survivors = wait_gone(pids)
    assert survivors == [], f"the ledger left processes running: {survivors}"
    assert runner.ledger.live_count() == 0


def test_the_working_directory_is_the_project(tmp_path):
    outcome = runner.run([sys.executable, "-c", "import os; print(os.getcwd())"],
                         tmp_path, "project")
    assert str(tmp_path.resolve()) in outcome.stdout


# ---------------------------------------------------------------------------
# No shells, ever
# ---------------------------------------------------------------------------

def test_shell_metacharacters_reach_the_program_as_literal_text(tmp_path):
    """Proof that there is no shell: a `;` is an argument, not a separator.

    If a shell were involved, the second command would run and the marker
    file would exist.
    """
    marker = tmp_path / "SHELL-RAN.txt"
    outcome = runner.run(
        [sys.executable, "-c", "import sys; print(repr(sys.argv[1:]))",
         f"; python -c \"open(r'{marker}','w').write('x')\""],
        tmp_path, "project",
    )
    assert not marker.exists(), "a shell interpreted the argument"
    assert ";" in outcome.stdout
