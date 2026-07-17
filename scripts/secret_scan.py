"""Secret scanner for CI — fails the build if a tracked file or a built
artifact appears to contain a real credential.

Two independent things this project has decided never to ship, checked
here for two different reasons:

1. **Tracked source files** (this script, run against ``git ls-files``)
   should never contain a real secret — that's a straightforward "this
   should never have been committed" bug, regardless of what gets built
   from it. ``tests/`` is excluded from the *failing* scan: this repo's
   security tests (see docs/SECURITY.md's "Privacy and data minimization"
   section) deliberately construct secret-*shaped* strings — an Anthropic
   key prefix followed by the alphabet, for example — to prove redaction
   actually works. That is the normal, expected shape of an adversarial
   test fixture, not a leak (and deliberately not spelled out literally
   right here, or this docstring would trip its own scanner). tests/ is
   still scanned, just in report-only mode, so a genuine accidental paste
   is still visible without making every new adversarial fixture a CI
   failure.

2. **Build artifacts** (the portable ZIP's contents, the installer's
   actually-installed output) get the *same* pattern set applied by
   ``installer/scan_artifacts.ps1`` in the Windows workflow — this script
   only covers the source tree; see that file for the packaged-output
   side.

Never prints a matched secret value — only the file, line number, and
which pattern matched. A GitHub Actions log is not a safe place to put the
very value this script exists to keep out of anything durable.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# (name, compiled pattern) — kept narrow and prefix-specific on purpose:
# broad heuristics (generic "looks like base64" or entropy checks) produce
# too many false positives on ordinary hashes/version strings/hex IDs to be
# useful as a hard CI gate. Every pattern here is a real, recognizable
# credential prefix or format.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("anthropic-api-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}")),
    ("generic-sk-api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("github-pat-classic", re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("github-pat-fine-grained", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("github-oauth-token", re.compile(r"gh[ousr]_[A-Za-z0-9]{36,}")),
    ("aws-access-key-id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    (
        "private-key-header",
        re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    # The literal header name with a real, token-shaped value attached —
    # not the bare header name alone, which legitimately appears throughout
    # this project's own source/docs/comments (including this file).
    ("jarvis-session-token", re.compile(r'X-Jarvis-Token["\'\]\s:=]{1,6}[A-Za-z0-9_-]{40,}', re.IGNORECASE)),
]

# installer/scan_artifacts.ps1 (the *built-artifact* scanner) additionally
# blocks on a Windows/Unix username embedded in a path
# ("[\\/](?:Users|home)[\\/]..."). That pattern is deliberately NOT mirrored
# here: PyInstaller output never legitimately contains prose *about* that
# pattern, but this repository's own tracked source does — extensively, in
# docs/SECURITY.md, app/core/redact.py's docstring, app/desktop/apps.py's
# real allowlisted paths, and several tests — so it would block on this
# project's own already-reviewed code, not on an actual leak. A real
# session token can never appear in a *build* artifact in the first place
# (see the jarvis-session-token comment above); a real, unexpected local
# absolute path in *tracked source* is a much rarer, much more ambiguous
# signal than in a compiled binary tree with no legitimate source of its own.

# Excluded wholesale: never source text, or (tests/) covered separately in
# report-only mode above.
_EXCLUDED_DIRS = {"tests", ".git", "__pycache__", ".pytest_cache", "data", "installer_output"}
_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".exe", ".dll",
    ".pyc", ".pyd", ".db", ".woff", ".woff2", ".ttf",
}


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    return [Path(p) for p in out.splitlines() if p.strip()]


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Returns [(line_number, pattern_name), ...] — never the matched text."""
    if path.suffix.lower() in _BINARY_SUFFIXES:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    findings = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for name, pattern in PATTERNS:
            if pattern.search(line):
                findings.append((line_no, name))
    return findings


def main() -> int:
    files = _tracked_files()
    blocking: list[tuple[Path, int, str]] = []
    report_only: list[tuple[Path, int, str]] = []

    for path in files:
        if not path.exists():
            continue  # deleted-but-still-staged edge case
        parts = set(path.parts)
        in_report_only_zone = "tests" in parts
        if not in_report_only_zone and parts & (_EXCLUDED_DIRS - {"tests"}):
            continue

        for line_no, pattern_name in _scan_file(path):
            entry = (path, line_no, pattern_name)
            (report_only if in_report_only_zone else blocking).append(entry)

    if report_only:
        print("Report-only matches in tests/ (adversarial fixtures expected here):")
        for path, line_no, pattern_name in report_only:
            print(f"  {path}:{line_no}  [{pattern_name}]")
        print()

    if blocking:
        print("BLOCKING: secret-like pattern(s) found in tracked source files:")
        for path, line_no, pattern_name in blocking:
            print(f"  {path}:{line_no}  [{pattern_name}]")
        print()
        print(
            "A matched value is never printed here. If this is a real credential, "
            "revoke it immediately (it is already in git history) before removing it."
        )
        return 1

    print(f"Secret scan clean: {len(files)} tracked files checked, no blocking matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
