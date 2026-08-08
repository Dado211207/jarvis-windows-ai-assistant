"""Sanity checks for scripts/build-installer.ps1.

PowerShell isn't installed in this project's Linux dev/CI sandbox, so
this cannot actually run the script — the same constraint documented on
tests/test_installer_script.py and tests/test_packaging_spec.py applies
here too. These are string-level checks for the properties that matter
most for a *build* script specifically: every one of its ten steps is
still present and in order, it fails fast (checks the exit code of every
native command it shells out to, since PowerShell's own
$ErrorActionPreference does not cover those), and it only ever deletes
inside packaging\\build / packaging\\dist. Real execution is verified on
windows-latest CI — see .github/workflows/windows-installer.yml and the
packaging report.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "build-installer.ps1"


def _read() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def test_file_exists():
    assert SCRIPT_PATH.exists()


def test_all_ten_steps_present_and_in_order():
    content = _read()
    step_numbers = [int(n) for n in re.findall(r'Write-Step (\d+)', content)]
    assert step_numbers == list(range(1, 11)), f"expected steps 1..10 in order, got {step_numbers}"


def test_fails_fast_on_any_error():
    content = _read()
    assert '$ErrorActionPreference = "Stop"' in content
    # $LASTEXITCODE is only meaningful right after a *native* command —
    # PowerShell's own $ErrorActionPreference doesn't cover those, so an
    # explicit check is required for every one this script shells out to.
    for native_command in ("pip install", "compileall", "pytest", "PyInstaller build", "ISCC"):
        assert f'Assert-LastExitCode "{native_command}"' in content or f"Assert-LastExitCode \"{native_command}" in content


def test_is_windows_only():
    content = _read()
    assert 'Windows_NT' in content


def test_cleans_only_the_packaging_build_output_directories():
    content = _read()
    # Exactly one recursive force-delete in the whole script, and it must
    # target the two $BuildDir/$DistDir variables (themselves built from
    # packaging\build and packaging\dist), never a bare/broad path.
    assert content.count("Remove-Item -Recurse -Force") == 1
    assert '"packaging\\build"' in content
    assert '"packaging\\dist"' in content


def test_pins_the_documented_pyinstaller_version():
    """docs/THIRD_PARTY_NOTICES.md documents PyInstaller as pinned to
    6.21.0 — this is the one place that pin must actually be enforced."""
    content = _read()
    assert "pyinstaller==6.21.0" in content


def test_pyinstaller_output_paths_match_installer_expectations():
    """packaging/jarvis.iss's [Files] Source ("dist\\JARVIS\\*") and
    OutputDir (dist\\installer) both resolve relative to jarvis.iss's own
    directory (packaging\\) — so the build must explicitly pin
    PyInstaller's --distpath/--workpath rather than rely on its default
    (relative to the *invoking* directory, not the .spec file's own
    directory), or the two would silently disagree about where the
    onedir build lives."""
    content = _read()
    assert '--distpath "packaging\\dist"' in content
    assert '--workpath "packaging\\build"' in content


def test_verifies_no_forbidden_files_in_build_output():
    content = _read()
    assert ".env" in content
    assert ".env.example" in content
    assert "*.db" in content


def test_computes_sha256_checksum():
    content = _read()
    assert "-Algorithm SHA256" in content
    assert ".sha256" in content


def test_no_secrets_referenced():
    content = _read()
    assert "ANTHROPIC_API_KEY" not in content
    assert "sk-" not in content
