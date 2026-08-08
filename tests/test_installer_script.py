"""Sanity checks for packaging/jarvis.iss.

Inno Setup's compiler (ISCC) is Windows-only — there is no way to
actually compile this script on Linux, matching this project's rule
against attempting to cross-compile the Windows side of packaging at
all. These are string/structure-level checks that catch the class of
regression that's cheap to catch without a real compile: a directive
this pass explicitly requires or forbids going missing or reappearing,
a path that stops matching what jarvis.spec actually produces, an
accidentally-reintroduced fake company string. Real compilation and
installer behavior is verified on windows-latest CI — see the
packaging report.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ISS_PATH = REPO_ROOT / "packaging" / "jarvis.iss"


def _read() -> str:
    return ISS_PATH.read_text(encoding="utf-8")


def _read_code_only() -> str:
    """Strips ';'-comment lines — for checks about what the script
    actually *does*, not what its own honest "deliberately does NOT..."
    documentation comments *mention* while explaining an exclusion.
    Directive lines never start with ';' in Inno Setup, so this is a
    precise, not approximate, filter."""
    return "\n".join(
        line for line in _read().splitlines()
        if not line.strip().startswith(";")
    )


def _section(name: str) -> str:
    """Returns the raw text of one [Section], up to the next [Section]
    or end of file — good enough for these directive-presence checks
    without a full Inno Setup parser."""
    content = _read()
    match = re.search(rf"^\[{re.escape(name)}\]\s*\n(.*?)(?=^\[|\Z)", content, re.MULTILINE | re.DOTALL)
    assert match, f"[{name}] section not found in jarvis.iss"
    return match.group(1)


def test_file_exists():
    assert ISS_PATH.exists()


# ---------------------------------------------------------------------------
# No admin/UAC requirement; per-user install
# ---------------------------------------------------------------------------

def test_no_admin_privileges_required_by_default():
    setup = _section("Setup")
    assert "PrivilegesRequired=lowest" in setup


def test_default_install_dir_is_per_user_localappdata():
    setup = _section("Setup")
    assert "DefaultDirName={localappdata}\\Programs\\{#MyAppName}" in setup


# ---------------------------------------------------------------------------
# AppId GUID formed correctly (regression guard for the brace-doubling
# bug caught during development — AppId={{#MyAppId} with an *unbraced*
# #define MyAppId, not both)
# ---------------------------------------------------------------------------

def test_app_id_constant_has_no_own_braces():
    content = _read()
    match = re.search(r'#define MyAppId "([^"]*)"', content)
    assert match, "MyAppId #define not found"
    assert not match.group(1).startswith("{"), "MyAppId must not include its own braces — AppId={{#MyAppId} already supplies them"


def test_app_id_directive_uses_the_escape_pattern():
    setup = _section("Setup")
    assert "AppId={{#MyAppId}" in setup


# ---------------------------------------------------------------------------
# Version / naming
# ---------------------------------------------------------------------------

def test_installer_version_matches_app_version():
    from app import __version__
    content = _read()
    assert f'#define MyAppVersion "{__version__}"' in content


def test_output_filename_matches_requested_pattern():
    setup = _section("Setup")
    assert "OutputBaseFilename=JARVIS-Setup-v{#MyAppVersion}-x64" in setup


# ---------------------------------------------------------------------------
# Shortcuts / tasks — desktop and startup opt-in, off by default; launch
# on finish opt-out, on by default
# ---------------------------------------------------------------------------

def test_desktop_shortcut_task_unchecked_by_default():
    tasks = _section("Tasks")
    desktop_line = next(l for l in tasks.splitlines() if l.strip().startswith('Name: "desktopicon"'))
    assert "Flags: unchecked" in desktop_line


def test_startup_task_unchecked_by_default():
    tasks = _section("Tasks")
    startup_line = next(l for l in tasks.splitlines() if l.strip().startswith('Name: "startupicon"'))
    assert "Flags: unchecked" in startup_line
    assert "sign in" in startup_line.lower() or "startup" in startup_line.lower()


def test_launch_on_finish_is_enabled_by_default():
    run_section = _section("Run")
    launch_line = next(l for l in run_section.splitlines() if "postinstall" in l)
    assert "unchecked" not in launch_line
    assert "skipifsilent" in launch_line  # never auto-launches during a silent/CI install


def test_start_menu_shortcut_is_unconditional():
    icons = _section("Icons")
    group_line = next(l for l in icons.splitlines() if l.strip().startswith('Name: "{group}\\{#MyAppName}"'))
    assert "Tasks:" not in group_line


# ---------------------------------------------------------------------------
# Uninstall data-removal: unchecked/no by default, scoped only to the
# validated AppData directory
# ---------------------------------------------------------------------------

def test_uninstall_data_removal_defaults_to_preserving_data_when_silent():
    code = _section("Code")
    assert "{param:DELETEDATA|no}" in code


def test_uninstall_data_removal_prompt_defaults_to_no_when_interactive():
    code = _section("Code")
    assert "MB_DEFBUTTON2" in code  # "No" is the focused/default button


def test_uninstall_only_targets_the_validated_jarvis_data_dir():
    code = _section("Code")
    assert "{localappdata}\\JARVIS" in code
    # Never a bare/broad {localappdata} deletion.
    assert "DelTree('{localappdata}'" not in code
    assert 'DelTree("{localappdata}"' not in code


def test_data_dir_is_the_sibling_of_the_install_dir_not_inside_it():
    """app_data_root() (%LOCALAPPDATA%\\JARVIS) and {app}
    (%LOCALAPPDATA%\\Programs\\JARVIS) must stay two separate trees —
    that separation is what makes upgrades safe by construction."""
    code = _section("Code")
    setup = _section("Setup")
    assert "{localappdata}\\JARVIS" in code
    assert "{localappdata}\\Programs\\{#MyAppName}" in setup


# ---------------------------------------------------------------------------
# Explicitly forbidden additions
# ---------------------------------------------------------------------------

def test_no_firewall_rule():
    content = _read_code_only().lower()
    assert "firewall" not in content
    assert "netsh" not in content


def test_no_windows_service_installation():
    content = _read_code_only()
    assert "Type: service" not in content
    assert "CreateService" not in content


def test_no_signtool_configured():
    """Unsigned build — no fake/self-signed certificate presented as
    trusted. See the packaging report for the real Authenticode result."""
    setup = _section("Setup")
    assert "SignTool" not in setup


def test_no_fake_company_strings():
    content = _read()
    for fake_sounding in ("Inc.", "LLC", "Corporation", "Ltd."):
        assert fake_sounding not in content
    assert "Dado211207" in content


def test_references_the_real_icon_file():
    from pathlib import Path as _Path
    setup = _section("Setup")
    assert "icon.ico" in setup
    assert (REPO_ROOT / "app" / "ui" / "static" / "icon.ico").exists()


# ---------------------------------------------------------------------------
# Consistency with packaging/jarvis.spec's actual output layout
# ---------------------------------------------------------------------------

def test_files_source_matches_expected_pyinstaller_distpath():
    files = _section("Files")
    assert 'Source: "dist\\JARVIS\\*"' in files
