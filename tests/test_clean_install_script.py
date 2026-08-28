"""Sanity checks for scripts/test_clean_install.py.

This script performs real, side-effecting Windows operations (silent
install/uninstall, launching the real frozen exe, taskkill) and can only
really run on windows-latest CI, immediately after
scripts/build-installer.ps1 has produced a real installer — see
.github/workflows/windows-installer.yml. These are structural checks
that catch the class of regression that's cheap to catch without
actually running it: a safety property silently regressing (e.g. a
force-kill creeping back in, or the explicit data-deletion opt-in
becoming implicit), a phase going missing, or the script losing its
non-Windows guard.
"""

import ast
import io
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "test_clean_install.py"


def _read() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def _code_only() -> str:
    """Strips real comment tokens (via the standard tokenizer, so an
    inline `# ...` trailing comment is handled correctly, not just a
    comment that happens to start a line) — for checks about what the
    script actually *does*, not prose (a comment, or this file's own
    module docstring) that happens to mention a flag while explaining
    why it's deliberately NOT used. The same false-positive class has
    bitten this project's other packaging tests more than once; this
    generalizes the fix for this file up front instead of waiting to
    rediscover it here too."""
    tokens = tokenize.generate_tokens(io.StringIO(_read()).readline)
    return "\n".join(tok.string for tok in tokens if tok.type != tokenize.COMMENT)


def test_file_exists():
    assert SCRIPT_PATH.exists()


def test_valid_python_syntax():
    ast.parse(_read())  # raises SyntaxError on failure


# ---------------------------------------------------------------------------
# Non-interactive, CI-safe install/uninstall
# ---------------------------------------------------------------------------

def test_uses_non_interactive_install_flags():
    content = _read()
    assert "/VERYSILENT" in content
    assert "/SUPPRESSMSGBOXES" in content


def test_never_force_kills_the_running_app():
    """The whole point of app/launcher/tray.py's WM_CLOSE handler is that
    a *graceful* stop (taskkill without /F) is enough to trigger clean
    shutdown. If this script ever force-kills instead, it would stop
    genuinely exercising that fix and silently paper over a regression
    of it."""
    code = _code_only()
    assert '"/F"' not in code
    assert "'/F'" not in code


# ---------------------------------------------------------------------------
# Data-removal opt-in stays explicit
# ---------------------------------------------------------------------------

def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _find_calls(func_def: ast.FunctionDef, func_name: str) -> list:
    """Every Call node invoking *func_name* inside *func_def* —
    deliberately narrower than dumping the whole function body, which
    would also match this file's own descriptive _step(...) banner text
    (e.g. phase_b's own "Silent uninstall (no /DELETEDATA flag)" step
    label) and produce exactly the false-positive/false-negative class
    documented on _code_only() above, just via a different route (a
    real string *argument* rather than a comment). A list, not a single
    node: phase_c calls run_silent() twice (reinstall, then uninstall
    with /DELETEDATA=yes) and only the second call is expected to carry
    the flag."""
    return [
        node for node in ast.walk(func_def)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == func_name
    ]


def test_default_uninstall_uses_no_deletedata_flag():
    tree = ast.parse(_read())
    phase_b = _find_function(tree, "phase_b_uninstall_preserves_data_by_default")
    calls = _find_calls(phase_b, "run_silent")
    assert calls, "expected phase_b to call run_silent()"
    assert all("DELETEDATA" not in ast.dump(call) for call in calls)


def test_explicit_deletedata_opt_in_is_used_in_phase_c():
    tree = ast.parse(_read())
    phase_c = _find_function(tree, "phase_c_reinstall_then_explicit_data_removal")
    calls = _find_calls(phase_c, "run_silent")
    assert calls, "expected phase_c to call run_silent()"
    assert any("/DELETEDATA=yes" in ast.dump(call) for call in calls)


# ---------------------------------------------------------------------------
# Install-dir removal must be polled, not checked exactly once
# ---------------------------------------------------------------------------

def test_uninstall_directory_removal_is_polled_not_checked_once():
    """Regression guard for a real CI failure: Inno Setup's uninstaller
    can't delete its own running .exe (confirmed against Inno Setup's own
    FAQ at jrsoftware.org/isfaq.php), so unins000.exe spawns a clone into
    %TEMP% that finishes the actual cleanup — deleting unins000.exe/.dat
    and removing the install directory — *after* signaling the originally
    invoked process to exit. subprocess.run() on the uninstaller can
    therefore return before that tail end of cleanup is actually done, so
    checking expected_install_dir().exists() exactly once immediately
    afterward is a real race, not a safety property worth failing fast on.
    Both phases that assert the install directory is gone must poll via
    wait_for_path_removed() instead."""
    tree = ast.parse(_read())
    for phase_name in (
        "phase_b_uninstall_preserves_data_by_default",
        "phase_c_reinstall_then_explicit_data_removal",
    ):
        phase = _find_function(tree, phase_name)
        calls = _find_calls(phase, "wait_for_path_removed")
        assert calls, f"expected {phase_name} to poll install-dir removal via wait_for_path_removed()"


# ---------------------------------------------------------------------------
# Install dir / data dir stay separate trees (mirrors
# tests/test_installer_script.py's equivalent guarantee for jarvis.iss)
# ---------------------------------------------------------------------------

def test_install_dir_and_data_dir_are_computed_as_separate_trees():
    content = _read()
    assert '"Programs" / "JARVIS"' in content
    assert 'Path(os.environ["LOCALAPPDATA"]) / "JARVIS"' in content


# ---------------------------------------------------------------------------
# Windows-only guard
# ---------------------------------------------------------------------------

def test_refuses_to_run_on_non_windows():
    content = _read()
    assert 'os.name != "nt"' in content


# ---------------------------------------------------------------------------
# Diagnosability: a health-wait failure must not be a dead end
# ---------------------------------------------------------------------------

def test_health_wait_failure_surfaces_the_apps_own_log():
    """Regression guard: an earlier real CI failure (a real frozen
    JARVIS.exe that launched, stayed running, and never answered
    /health) produced zero diagnostic trail beyond "never became
    healthy" — the actual cause required guessing from precedent rather
    than evidence. wait_for_health()'s failure path must read and
    include the installed app's own log file, not just report the
    timeout."""
    tree = ast.parse(_read())
    wait_for_health = _find_function(tree, "wait_for_health")
    calls = _find_calls(wait_for_health, "_fail")
    assert calls, "expected wait_for_health() to call _fail()"
    assert any("log_tail" in ast.dump(call) for call in calls)
    assert any("trace_tail" in ast.dump(call) for call in calls), (
        "a health-wait failure must also surface app/launcher/boot_trace.py's "
        "own trace file — added after jarvis.log itself turned up completely "
        "empty on a real CI failure, meaning the problem was before any "
        "logger.*() call ever ran"
    )


# ---------------------------------------------------------------------------
# All three phases exist and main() runs them in order
# ---------------------------------------------------------------------------

def test_all_three_phases_are_defined():
    tree = ast.parse(_read())
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    for expected in (
        "phase_a_install_launch_and_stop",
        "phase_b_uninstall_preserves_data_by_default",
        "phase_c_reinstall_then_explicit_data_removal",
        "phase_h_upgrade_from_a_v01_zip_install",
    ):
        assert expected in defined


def test_main_calls_every_phase_in_the_only_order_that_works():
    """The order is a dependency, not a preference.

    The lifecycle loop needs an installed application, so it has to run
    after the install phase and before the uninstall phases — running it
    last would mean starting an application that had just been removed.
    The voice-chain phase downloads two models into the data directory,
    so it has to run before the phase that deletes that directory.

    The Coding Workspace phase has the same dependency as the lifecycle
    loop — it drives the installed executable — and runs before it so that
    a browser or preview process it failed to clean up would be caught by
    the ten start/quit cycles that follow, rather than after them where
    nothing would look.

    The upgrade phase is last for the opposite reason: the v0.1 migration
    only fires on a machine with no JARVIS data, which is precisely the
    state phase C leaves behind.
    """
    tree = ast.parse(_read())
    main_fn = _find_function(tree, "main")
    call_names = [
        n.func.id for n in ast.walk(main_fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    phase_calls = [n for n in call_names if n.startswith("phase_")]
    assert phase_calls == [
        "phase_a_install_launch_and_stop",
        "phase_f_installed_runtime_selftest",
        "phase_g_real_voice_through_the_installed_product",
        "phase_i_installed_coding_workspace",
        "phase_d_repeated_start_and_quit",
        "phase_e_repeated_restart",
        "phase_b_uninstall_preserves_data_by_default",
        "phase_c_reinstall_then_explicit_data_removal",
        "phase_h_upgrade_from_a_v01_zip_install",
    ]


def test_the_upgrade_phase_uses_a_real_candidate_location_not_the_override():
    """`JARVIS_LEGACY_DB` would prove only that a path handed in can be
    read. The point of the packaged test is that the application finds a
    v0.1 install on its own."""
    code = _code_only()

    assert "legacy_zip_db_path" in code
    assert "JARVIS_LEGACY_DB" not in code, "the packaged test must not use the override"
    assert "Downloads" in code


def test_the_upgrade_phase_proves_idempotence_and_preservation():
    tree = ast.parse(_read())
    body = ast.unparse(_find_function(tree, "phase_h_upgrade_from_a_v01_zip_install"))

    assert "read_bytes" in body, "the legacy file must be compared byte-for-byte"
    assert body.count("wait_for_desktop_ready") >= 2, "a restart must be part of the phase"
    assert "duplicated" in body or "idempotent" in body


def test_the_upgrade_phase_cleans_up_only_what_it_created():
    """Removing a real v0.1 install that happened to be on the machine
    would be the worst possible outcome of a test about preserving it."""
    tree = ast.parse(_read())
    body = ast.unparse(_find_function(tree, "phase_h_upgrade_from_a_v01_zip_install"))

    assert "created_legacy_root" in body
    assert "already exists" in body, "an existing legacy install must be left alone"


def test_restart_is_exercised_enough_times_and_proves_replacement():
    """A restart that reused the old process would pass a bare "is it
    healthy" check. What has to be true is that the previous runtime is
    gone and a different one answers."""
    import re

    content = _read()
    match = re.search(r"^RESTART_CYCLES\s*=\s*(\d+)", content, re.MULTILINE)

    assert match, "the restart loop must declare how many cycles it runs"
    assert int(match.group(1)) >= 10
    assert "pid did not change" in content, "a reused process must be caught"
    assert "is still running after the restart" in content, "an orphaned previous runtime must be caught"


def test_the_lifecycle_loop_runs_enough_times_to_prove_nothing_accumulates():
    """One start/stop is a happy path. The failures this catches — a port
    still held, a WebView2 process that outlived its parent — are the
    ones that only appear after repetition."""
    import re

    content = _read()
    match = re.search(r"^LIFECYCLE_CYCLES\s*=\s*(\d+)", content, re.MULTILINE)

    assert match, "the lifecycle loop must declare how many cycles it runs"
    assert int(match.group(1)) >= 10


def test_each_lifecycle_cycle_checks_for_orphans_and_a_released_port():
    """Asserting only that the process exited would pass while leaving a
    WebView2 process running and the port held."""
    content = _read()

    assert "msedgewebview2.exe" in content, "orphaned WebView2 processes must be checked for"
    assert "_wait_for_port_release" in content
    assert "_jarvis_processes" in content


def test_the_lifecycle_cycle_does_not_pass_merely_because_jarvis_exe_exited():
    """The exact hole this suite exists to keep closed.

    "JARVIS.exe is gone" was the only thing every check before the
    ten-cycle test asked, and it is true of a shutdown that leaves a
    WebView2 browser process running — which is what a user sees in Task
    Manager and calls "JARVIS is still running". The cycle must assert
    all four: the process, the health endpoint, the port, and the
    processes JARVIS started.
    """
    tree = ast.parse(_read())
    body = ast.unparse(_find_function(tree, "phase_d_repeated_start_and_quit"))

    for required in (
        "wait_for_pid_exit",              # the process itself
        "wait_for_health_to_stop",        # it stopped serving
        "_wait_for_port_release",         # it let go of the port
        "_wait_for_identities_to_exit",   # and its WebView2 children are gone
    ):
        assert required in body, f"a lifecycle cycle that never calls {required} proves too little"


def test_orphan_detection_waits_for_captured_identities_not_a_single_pid_sample():
    """Two defects in one line, both fixed here.

    `psutil.pid_exists(pid)` asked whether *some* process holds that
    number — which a recycled PID answers wrongly — and asked it exactly
    once, with no allowance for the operating system finishing a
    teardown JARVIS had already ordered. The replacement waits for the
    exact captured identities, so it can neither be fooled by a recycled
    PID nor fail a product that did its job.
    """
    code = _code_only()

    assert "_wait_for_identities_to_exit" in code
    assert "create_time" in code, "an identity check needs more than a PID"
    # The bare single-sample check must not come back.
    assert "psutil.pid_exists(pid) for pid in" not in code
    assert "orphaned_webviews" not in code


def test_the_orphan_wait_is_bounded_and_short():
    """A generous wait would turn a real leak into a slow pass. This one
    only covers the tail of a teardown JARVIS already performed, so it is
    deliberately close to process_tree's own worst case."""
    import re

    match = re.search(r"^WEBVIEW_SETTLE_TIMEOUT_SECONDS\s*=\s*([\d.]+)", _read(), re.MULTILINE)
    assert match, "the settle wait must declare its bound"

    from app.launcher import process_tree

    product_worst_case = process_tree.TERMINATE_GRACE_SECONDS + process_tree.KILL_GRACE_SECONDS
    settle = float(match.group(1))
    assert settle >= product_worst_case, "the wait must at least cover the cleanup it follows"
    assert settle <= product_worst_case * 2, (
        f"{settle}s is far longer than the {product_worst_case}s of cleanup it is waiting on; "
        "a leak would have time to look like a pass"
    )


def test_a_failed_cycle_collects_the_evidence_needed_to_diagnose_it():
    """The first WebView2 orphan produced a one-line failure and an
    artifact containing the installer log and two self-test logs — not
    the window child's log, which is the one that would have named the
    cause."""
    code = _code_only()

    assert "_collect_application_logs" in code
    assert "expected_window_log_file" in code
    assert "_describe" in code, "a survivor must be named, not counted"


# ---------------------------------------------------------------------------
# Real traffic against the installed app, not just process-exists checks
# ---------------------------------------------------------------------------

def test_verifies_dashboard_html_and_a_static_asset():
    content = _read()
    assert "/ui/setup" in content
    assert "/ui/static/" in content
    assert "text/html" in content
    assert "text/css" in content


def test_verifies_single_instance_behavior():
    content = _read()
    assert "second" in content.lower()
    assert "expected 0" in content or "code 0" in content.lower()


# ---------------------------------------------------------------------------
# No secrets
# ---------------------------------------------------------------------------

def test_no_secrets_referenced():
    content = _read()
    assert "ANTHROPIC_API_KEY" not in content
    assert "sk-" not in content


# ---------------------------------------------------------------------------
# The Coding Workspace acceptance phase
# ---------------------------------------------------------------------------

def _acceptance_source() -> str:
    return (Path(__file__).resolve().parents[1]
            / "scripts" / "installed_coding_acceptance.py").read_text(encoding="utf-8")


def test_the_acceptance_phase_derives_the_base_url_rather_than_hardcoding_one():
    """A hardcoded `http://127.0.0.1:8000` sent every request in this phase
    to a port nothing was listening on, while the installed application was
    perfectly healthy on the port `app.config.settings` names.

    The failure read as "the app died immediately after reporting ready",
    which is a much more alarming thing than it was — and cost two Windows
    installer runs to diagnose. There is one source for the address, and
    this is the test that keeps it that way.
    """
    import ast

    source = _acceptance_source()
    tree = ast.parse(source)
    base = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "BASE_URL" for t in node.targets)
    ]
    assert len(base) == 1, "BASE_URL must be assigned exactly once"
    rendered = ast.unparse(base[0].value)
    assert "settings.jarvis_host" in rendered, rendered
    assert "settings.jarvis_port" in rendered, rendered

    # Structural, not textual: a grep for the offending port matched the
    # comment explaining why the port must not be written here — the same
    # self-matching-grep class this repository has fixed twice before. Only
    # actual string literals are inspected.
    import re

    literals = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    hardcoded = [text for text in literals
                 if re.search(r"https?://[^\s\"]*:\d+", text)]
    assert hardcoded == [], f"a URL with a hardcoded port appears in {hardcoded}"


def test_the_acceptance_phase_reviews_the_preview_plan_before_starting_it():
    """Keep the installed-product test on the same two-step API as the UI.

    The preview endpoint accepts only a short-lived reviewed ``plan_id``.
    Posting ``project_id`` directly reached main unnoticed and made the real
    clean-install run fail with HTTP 422 after the installer itself succeeded.
    """
    import ast

    tree = ast.parse(_acceptance_source())
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_start_preview"
    )

    posts = {}
    for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
        if not (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "post"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ):
            continue
        json_keyword = next((kw for kw in call.keywords if kw.arg == "json"), None)
        assert json_keyword is not None and isinstance(json_keyword.value, ast.Dict)
        posts[call.args[0].value] = {
            key.value
            for key in json_keyword.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }

    assert posts["/coding/preview/plan"] == {"project_id", "script"}
    assert posts["/coding/preview/start"] == {"plan_id"}


def test_the_acceptance_phase_asserts_on_real_counts_not_a_bare_total():
    """A check that found the two <h1>s and nothing else would satisfy
    `problem_count > 0` while missing the console error, the broken image
    and the overflow entirely."""
    source = _acceptance_source()
    for expected in ('"http_status": 200', '"h1_count": 2',
                     '"console_errors": 1', '"broken_images": 1'):
        assert expected in source, f"the defective-fixture assertion lost {expected}"
    assert "problem_count > 0" not in source


def test_the_acceptance_phase_fails_when_no_browser_ran():
    """It must fail if the engine is unavailable, if the check was skipped,
    or if Playwright turns out to be inside the packaged application."""
    source = _acceptance_source()
    assert "engine_unavailable" in source
    assert 'findings.get("opened")' in source
    assert "playwright" in source.lower()
