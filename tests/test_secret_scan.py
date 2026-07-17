"""Tests for scripts/secret_scan.py — the tracked-files secret scanner run
in CI (ci.yml). Loaded directly by file path (it's a standalone script, not
a package under app/), so these tests exercise the exact code CI runs.

See app/core/redact.py and tests/test_diagnostics.py for the companion
scanner over user-facing *output* text; this one covers tracked *source*
files, and installer/scan_artifacts.ps1 (not unit-tested here — no
PowerShell runtime in this suite) covers *built artifacts*.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "secret_scan.py"
_spec = importlib.util.spec_from_file_location("secret_scan", _SCRIPT_PATH)
secret_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(secret_scan)


# --- PATTERNS: adversarial matching, mirrors test_diagnostics.py's redact_text coverage ---

@pytest.mark.parametrize("text", [
    "key = sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
    "OPENAI_KEY=sk-abcdefghijklmnopqrstuvwxyz1234",
    "token: ghp_" + "a" * 36,
    "github_pat_" + "a" * 30,
    "oauth: ghu_" + "a" * 40,
    "AKIAABCDEFGHIJKLMNOP",
    "xoxb-1234567890-abcdefghij",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
])
def test_patterns_catch_known_secret_shapes(text):
    assert any(pattern.search(text) for _, pattern in secret_scan.PATTERNS)


@pytest.mark.parametrize("text", [
    "Version: 0.1.7-alpha",
    "assistant_name = JARVIS",
    "a normal sentence with no secrets in it at all",
    "sk-short",  # too short to match either sk- pattern
    "def process(self, command: str) -> CommandResponse:",
])
def test_patterns_do_not_false_positive_on_ordinary_text(text):
    assert not any(pattern.search(text) for _, pattern in secret_scan.PATTERNS)


# --- _scan_file(): per-file line/pattern reporting, never the matched value ---

def test_scan_file_reports_line_and_pattern_not_value(tmp_path):
    f = tmp_path / "leaked.py"
    f.write_text('API_KEY = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"\n')
    findings = secret_scan._scan_file(f)
    assert findings == [(1, "anthropic-api-key"), (1, "generic-sk-api-key")]


def test_scan_file_clean_file_reports_nothing(tmp_path):
    f = tmp_path / "clean.py"
    f.write_text("def hello():\n    return 'world'\n")
    assert secret_scan._scan_file(f) == []


def test_scan_file_skips_binary_extensions(tmp_path):
    f = tmp_path / "image.png"
    f.write_bytes(b"sk-ant-api03-abcdefghijklmnopqrstuvwxyz")  # secret-shaped bytes, wrong extension
    assert secret_scan._scan_file(f) == []


def test_scan_file_handles_undecodable_bytes_without_raising(tmp_path):
    f = tmp_path / "weird.py"
    f.write_bytes(b"\xff\xfe\x00\x01 not valid utf-8")
    assert secret_scan._scan_file(f) == []


# --- main(): report-only tests/ vs blocking everywhere else, via a real git repo ---

def _init_repo_with_files(tmp_path, files: dict):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    for rel_path, content in files.items():
        full = tmp_path / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)


def test_main_blocks_on_secret_in_non_test_file(tmp_path):
    _init_repo_with_files(tmp_path, {
        "app/leaked.py": 'KEY = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"\n',
    })
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "BLOCKING" in result.stdout
    assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in result.stdout
    assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in result.stderr


def test_main_does_not_block_on_secret_shaped_string_in_tests_dir(tmp_path):
    _init_repo_with_files(tmp_path, {
        "tests/test_fake.py": 'FAKE_KEY = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"  # adversarial fixture\n',
        "app/clean.py": "def ok(): return True\n",
    })
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "Report-only matches in tests/" in result.stdout
    assert "BLOCKING" not in result.stdout


def test_main_clean_repo_exits_zero(tmp_path):
    _init_repo_with_files(tmp_path, {"README.md": "# Nothing to see here\n"})
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "Secret scan clean" in result.stdout


def test_real_repo_secret_scan_passes():
    """Runs the actual script against this actual repo — a regression
    guard against a real secret ever landing in a tracked source file."""
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)], cwd=repo_root, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
