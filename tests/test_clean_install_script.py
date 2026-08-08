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
# All three phases exist and main() runs them in order
# ---------------------------------------------------------------------------

def test_all_three_phases_are_defined():
    tree = ast.parse(_read())
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    for expected in (
        "phase_a_install_launch_and_stop",
        "phase_b_uninstall_preserves_data_by_default",
        "phase_c_reinstall_then_explicit_data_removal",
    ):
        assert expected in defined


def test_main_calls_all_three_phases_in_order():
    tree = ast.parse(_read())
    main_fn = _find_function(tree, "main")
    call_names = [
        n.func.id for n in ast.walk(main_fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    phase_calls = [n for n in call_names if n.startswith("phase_")]
    assert phase_calls == [
        "phase_a_install_launch_and_stop",
        "phase_b_uninstall_preserves_data_by_default",
        "phase_c_reinstall_then_explicit_data_removal",
    ]


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
