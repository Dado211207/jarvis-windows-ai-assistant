"""The command runner: what the child sees, and whether it really dies.

Three failures this guards against, all of which look fine from the
outside:

* A child process inherits the parent's environment and therefore the
  user's Anthropic API key. `npm install` running a postinstall script
  would have it.
* "Killed" means `kill()` did not raise, rather than the process being
  gone. A dev server that spawned a child outlives the task, holds the
  port, and the next preview mysteriously fails.
* The program is handed to `CreateProcess` as a bare name. On Windows
  `npm` is `npm.cmd` and `CreateProcess` does not apply `PATHEXT`, so
  every Node command in the product raised `FileNotFoundError` — one
  line after the toolchain page reported `npm=available`, because
  `shutil.which` *does* apply it. See the `_resolved_argv` section.
"""

import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from app.coding import limits, runner
from app.coding.runner import CommandHandle, build_environment, redacted_environment_summary
from tests import coding_fixtures as fx


def make_program(directory: Path, name: str, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    """A real executable file, so `shutil.which` genuinely finds it."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


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


# ---------------------------------------------------------------------------
# Resolving the program — the Windows defect the packaged test found
# ---------------------------------------------------------------------------
#
# `subprocess.Popen(..., shell=False)` calls `CreateProcess`, which does
# NOT apply `PATHEXT`. `npm`, `npx`, `yarn` and `pnpm` are all `.cmd`
# shims on Windows, so `["npm", "run", "dev"]` raised `FileNotFoundError`
# on every Windows machine — while `app/coding/toolchain.py` reported
# `npm=available`, because `shutil.which` applies `PATHEXT` and
# `CreateProcess` does not. The installed acceptance test caught it at
# step 11: `npm=available` on one line, "the preview could not start" on
# the next.

def handle_for(tmp_path: Path, argv) -> CommandHandle:
    return CommandHandle(argv, tmp_path, "project")


def test_a_bare_program_name_becomes_an_absolute_path(tmp_path):
    """The fix, in one assertion: what reaches Popen is a file, not a name.

    A bare name is what `CreateProcess` cannot extend; an absolute path
    to the real file is what it can always start.
    """
    bindir = tmp_path / "bin"
    program = make_program(bindir, "faketool")
    project = tmp_path / "project"
    project.mkdir()

    resolved = handle_for(project, ["faketool", "--version"])._resolved_argv(
        {"PATH": str(bindir)})

    assert os.path.isabs(resolved[0]), "the program was still a bare name"
    assert Path(resolved[0]) == program.resolve()


def test_a_windows_style_shim_is_what_gets_started(tmp_path):
    """`npm` must reach Popen as `npm.cmd`.

    `shutil.which` only consults `PATHEXT` on Windows, so the platform
    lookup is emulated here rather than faked into existence on Linux.
    What is being proven is the runner's half of the contract: whatever
    file `which` names for a bare program is the file handed to Popen —
    which is precisely what turns `npm` into `npm.cmd` on Windows.
    """
    bindir = tmp_path / "bin"
    shim = make_program(bindir, "npm.cmd")
    project = tmp_path / "project"
    project.mkdir()

    def which_with_pathext(cmd, mode=os.F_OK | os.X_OK, path=None):
        return str(shim) if cmd == "npm" else None

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(shutil, "which", which_with_pathext)
        resolved = handle_for(project, ["npm", "run", "dev"])._resolved_argv(
            {"PATH": str(bindir)})

    assert Path(resolved[0]) == shim.resolve()
    assert resolved[0].endswith(".cmd")
    assert resolved[1:] == ["run", "dev"], "the arguments were disturbed"


def test_resolution_uses_the_childs_path_not_this_processs(tmp_path, monkeypatch):
    """What is looked up must be what the child would have looked up.

    Resolving against `os.environ` would find a tool the child cannot see
    — the runner deliberately hands the child a reduced PATH.
    """
    child_dir = tmp_path / "child-bin"
    parent_dir = tmp_path / "parent-bin"
    make_program(child_dir, "childtool")
    make_program(parent_dir, "parenttool")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("PATH", str(parent_dir))

    child_env = {"PATH": str(child_dir)}
    assert os.path.isabs(
        handle_for(project, ["childtool"])._resolved_argv(child_env)[0])
    # Only on the parent's PATH: not resolvable for the child, and left
    # alone so Popen produces the familiar "not installed" message.
    assert handle_for(project, ["parenttool"])._resolved_argv(child_env) == ["parenttool"]


def test_an_executable_inside_the_project_is_refused_not_run(tmp_path):
    """A `git.exe` committed to a repository is a repository choosing
    which Git inspects it. `toolchain.py` refuses to *probe* one; here it
    would actually be executed, so the refusal has to be louder."""
    project = tmp_path / "project"
    make_program(project / "tools", "git")

    with pytest.raises(PermissionError) as caught:
        handle_for(project, ["git", "status"])._resolved_argv(
            {"PATH": str(project / "tools")})

    message = str(caught.value)
    assert "git" in message
    assert "inside this project" in message


def test_a_symlink_into_the_project_is_refused_too(tmp_path):
    """Component-wise on the *resolved* path, so a link planted on PATH
    cannot launder a project-supplied executable."""
    project = tmp_path / "project"
    real = make_program(project / "tools", "vite")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    try:
        (bindir / "vite").symlink_to(real)
    except (OSError, NotImplementedError):  # pragma: no cover — no symlink privilege
        pytest.skip("this platform/account cannot create symlinks")

    with pytest.raises(PermissionError):
        handle_for(project, ["vite"])._resolved_argv({"PATH": str(bindir)})


def test_a_project_supplied_program_is_named_not_reduced_to_an_error_class(tmp_path):
    """End to end: "could not be started (PermissionError)" tells the user
    nothing. "JARVIS will not run an executable a repository supplied"
    tells them what happened and why."""
    project = tmp_path / "project"
    make_program(project / "tools", "sneaky")

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PATH", str(project / "tools"))
        outcome = runner.run(["sneaky"], project, "project")

    assert outcome.ok is False
    assert outcome.exit_code is None
    assert "repository supplied" in outcome.stderr
    assert "PermissionError" not in outcome.stderr


def test_an_absolute_program_passes_through_untouched(tmp_path):
    """Nothing to look up, and no second guess at a caller's explicit path."""
    project = tmp_path / "project"
    project.mkdir()
    argv = [sys.executable, "-c", "pass"]
    assert handle_for(project, argv)._resolved_argv({"PATH": "/nonexistent"}) == argv


def test_a_relative_path_program_passes_through_untouched(tmp_path):
    """`commands.classify()` already refuses any argv[0] containing a
    separator, so this branch never sees an attacker-supplied path. It is
    a passthrough by design: `_resolved_argv` resolves names, and does not
    become a second path-checking routine — that is how `canonical_root()`
    and `resolve()` drifted apart once already."""
    from app.coding import commands

    project = tmp_path / "project"
    project.mkdir()
    argv = ["./node_modules/.bin/vite", "build"]
    assert handle_for(project, argv)._resolved_argv({"PATH": "/nonexistent"}) == argv
    assert commands.classify(argv).tier == commands.CommandTier.BLOCKED


def test_a_missing_program_keeps_the_message_the_user_already_reads(tmp_path):
    """Left as-is so Popen raises, so `run()` produces the message it
    always did. Resolving must not turn a clear "not installed" into a
    silent difference in behaviour."""
    project = tmp_path / "project"
    project.mkdir()
    argv = ["definitely-not-a-real-program-xyz", "--help"]
    assert handle_for(project, argv)._resolved_argv({"PATH": str(tmp_path)}) == argv

    outcome = runner.run(argv, project, "project")
    assert "is not installed, or is not on PATH" in outcome.stderr


def test_an_env_with_no_path_at_all_does_not_raise(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    assert handle_for(project, ["anything"])._resolved_argv({}) == ["anything"]


def test_empty_argv_is_returned_unchanged(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    assert handle_for(project, [])._resolved_argv({"PATH": str(tmp_path)}) == []


def test_an_unresolvable_project_root_still_starts_the_program(tmp_path):
    """A project root that cannot be canonicalised is a reason to skip the
    containment comparison, never a reason to fail to run a tool that is
    plainly on PATH somewhere else entirely."""
    bindir = tmp_path / "bin"
    program = make_program(bindir, "faketool")

    class Unresolvable(type(Path())):  # type: ignore[misc]
        def resolve(self, strict=False):  # noqa: D102
            raise OSError("cannot canonicalise")

    handle = CommandHandle(["faketool"], Unresolvable(tmp_path / "project"), "project")
    resolved = handle._resolved_argv({"PATH": str(bindir)})
    assert Path(resolved[0]).name == program.name


def test_resolving_never_produces_a_command_line(tmp_path):
    """argv only, still. The resolved program is one element of a list;
    nothing here joins, quotes or interpolates."""
    bindir = tmp_path / "bin"
    make_program(bindir, "faketool")
    project = tmp_path / "project"
    project.mkdir()

    argv = ["faketool", "arg with spaces", "&& echo pwned", "$(id)"]
    resolved = handle_for(project, argv)._resolved_argv({"PATH": str(bindir)})

    assert isinstance(resolved, list)
    assert all(isinstance(part, str) for part in resolved)
    assert resolved[1:] == argv[1:], "an argument was quoted, escaped or merged"


def test_a_tool_the_toolchain_calls_available_can_actually_be_started(tmp_path):
    """The defect in one sentence: the toolchain page and the runner
    disagreed about whether a program existed. They must agree."""
    from app.coding import toolchain

    project = tmp_path / "project"
    project.mkdir()
    env = build_environment()

    checked = 0
    for spec in toolchain.TOOLS:
        if toolchain.probe(spec).state != toolchain.AVAILABLE:
            continue
        checked += 1
        startable = []
        for executable in spec.executables:
            resolved = handle_for(project, [executable, "--version"])._resolved_argv(env)
            if os.path.isabs(resolved[0]) and Path(resolved[0]).exists():
                startable.append(executable)
        assert startable, (
            f"{spec.display} is reported available, but none of {list(spec.executables)} "
            "would reach CreateProcess as anything but a bare name — the exact "
            "mismatch this resolution exists to remove"
        )

    if checked == 0:  # pragma: no cover — a machine with no toolchain at all
        pytest.skip("no development tool was detected on this machine")
