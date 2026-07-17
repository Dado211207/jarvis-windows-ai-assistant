"""Executes the real installer/scan_artifacts.ps1 against controlled clean
and intentionally contaminated fixture directories, via the real PowerShell
parser and runtime (subprocess to `pwsh`) — not a re-implementation of its
logic in Python, and not a syntax-only check.

GitHub-hosted ubuntu-latest runners ship pwsh by default, so this runs for
real in CI (ci.yml) on every push, not just when someone happens to be
testing on Windows. Skips (not fails) when pwsh isn't on PATH, since this
is genuinely optional locally — but wherever it does run, it is exercising
the actual script byte-for-byte, the same file windows-build.yml invokes.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "installer" / "scan_artifacts.ps1"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh not installed on this machine")


def _run_scan(path: Path, label: str = "test", allow_portable_extras: bool = False):
    args = [PWSH, "-NoProfile", "-File", str(SCRIPT), "-Path", str(path), "-Label", label]
    if allow_portable_extras:
        args.append("-AllowPortableExtras")
    return subprocess.run(args, capture_output=True, text=True)


def _make_clean_app(root: Path) -> Path:
    app = root / "app"
    app.mkdir(parents=True)
    (app / "JARVIS.exe").write_text("binary-placeholder")
    (app / "readme.txt").write_text("a normal readme")
    return app


def test_script_parses_with_the_real_powershell_parser():
    """The authoritative syntax check — the actual PowerShell language
    parser, not a manual read-through."""
    result = subprocess.run(
        [
            PWSH, "-NoProfile", "-Command",
            "$e=$null;$t=$null;"
            f"[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',[ref]$t,[ref]$e)|Out-Null;"
            "if ($e.Count -eq 0) { exit 0 } else { $e | ForEach-Object { Write-Host $_.Message }; exit 1 }",
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_clean_artifact_passes(tmp_path):
    app = _make_clean_app(tmp_path)
    result = _run_scan(app)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Artifact scan passed" in result.stdout


def test_rejects_env_file(tmp_path):
    """Regression guard: Get-ChildItem without -Force silently excludes
    dotfiles on at least one real PowerShell runtime, which would have made
    this exact check never see a real .env at all — see the comment above
    the -Force flags in scan_artifacts.ps1."""
    app = _make_clean_app(tmp_path)
    (app / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-shouldneverbehere")
    result = _run_scan(app)
    assert result.returncode != 0
    assert ".env" in result.stdout
    assert "sk-ant-shouldneverbehere" not in result.stdout
    assert "sk-ant-shouldneverbehere" not in result.stderr


def test_rejects_jarvis_db(tmp_path):
    app = _make_clean_app(tmp_path)
    (app / "data").mkdir()
    (app / "data" / "jarvis.db").write_bytes(b"sqlite-placeholder")
    result = _run_scan(app)
    assert result.returncode != 0
    assert "jarvis.db" in result.stdout


def test_rejects_log_files(tmp_path):
    app = _make_clean_app(tmp_path)
    (app / "data" / "logs").mkdir(parents=True)
    (app / "data" / "logs" / "jarvis.log").write_text("2026-07-17 a log line")
    result = _run_scan(app)
    assert result.returncode != 0
    assert ".log" in result.stdout


def test_rejects_api_key_like_content_without_printing_it(tmp_path):
    app = _make_clean_app(tmp_path)
    secret = "sk-ant-api03-REALLOOKINGSECRETVALUE1234567890"
    (app / "config.txt").write_text(f'ANTHROPIC_API_KEY = "{secret}"')
    result = _run_scan(app)
    assert result.returncode != 0
    assert "anthropic-api-key" in result.stdout
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_rejects_session_token_like_content_without_printing_it(tmp_path):
    app = _make_clean_app(tmp_path)
    token = "4f8x9zK2mP7qR1wS6tY0uV3bN5cE8dA2gH4jL7kM9nQ1"
    (app / "debug.txt").write_text(f"X-Jarvis-Token: {token}")
    result = _run_scan(app)
    assert result.returncode != 0
    assert "jarvis-session-token" in result.stdout
    assert token not in result.stdout
    assert token not in result.stderr


def test_does_not_flag_bare_mention_of_the_token_header_name(tmp_path):
    """The header name alone (no value attached) is not a leak — it
    legitimately appears throughout this project's own docs/comments."""
    app = _make_clean_app(tmp_path)
    (app / "notes.txt").write_text("Pass it as the X-Jarvis-Token header on state-changing requests.")
    result = _run_scan(app)
    assert result.returncode == 0, result.stdout + result.stderr


def test_rejects_absolute_windows_user_path_without_printing_it(tmp_path):
    app = _make_clean_app(tmp_path)
    username = "jsmith"
    (app / "buildinfo.txt").write_text(f"build root was C:\\Users\\{username}\\source\\repos\\jarvis")
    result = _run_scan(app)
    assert result.returncode != 0
    assert "windows-or-unix-user-path" in result.stdout
    assert username not in result.stdout
    assert username not in result.stderr


def test_returns_nonzero_exit_code_on_any_failure(tmp_path):
    app = _make_clean_app(tmp_path)
    (app / ".env").write_text("x")
    result = _run_scan(app)
    assert result.returncode == 1


def test_portable_extras_rejected_without_the_flag(tmp_path):
    app = _make_clean_app(tmp_path)
    (app / ".env.example").write_text("ANTHROPIC_API_KEY=your-key-here")
    (app / "START_JARVIS.bat").write_text("@echo off")
    result = _run_scan(app, allow_portable_extras=False)
    assert result.returncode != 0
    assert ".env.example" in result.stdout


def test_portable_extras_allowed_with_the_flag(tmp_path):
    app = _make_clean_app(tmp_path)
    (app / ".env.example").write_text("ANTHROPIC_API_KEY=your-key-here")
    (app / "START_JARVIS.bat").write_text("@echo off")
    result = _run_scan(app, allow_portable_extras=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_dev_setup_script_forbidden_even_with_portable_extras_flag(tmp_path):
    app = _make_clean_app(tmp_path)
    (app / "DEV_SETUP_FROM_SOURCE.bat").write_text("@echo dev setup")
    result = _run_scan(app, allow_portable_extras=True)
    assert result.returncode != 0
    assert "DEV_SETUP_FROM_SOURCE.bat" in result.stdout
