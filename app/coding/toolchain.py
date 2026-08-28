"""What is installed on this machine, and what cannot run without it.

A task that fails because `npm` is not installed should say so before it
starts, not produce a command that exits 9009 with a message the user has
to interpret. And a validation step that could not run because its tool
is missing must **never** be reported as passed — that is the same class
of lie as reporting zero console errors for a page nothing opened.

**Detection changes nothing.** Every probe is `--version` on a resolved
executable. Nothing is installed, nothing is configured, no PATH is
written, no registry key is touched.

**Bounded lookup, never a disk scan.** `PATH` resolution plus a small,
named set of standard install locations — the same rule
`app/core/legacy_migration.py` follows ("look, do not search"), enforced
by a test that walks this module's AST for `os.walk` and `rglob`.

**A project file may not impersonate a tool.** On Windows, `PATH`
resolution has historically consulted the current directory, and a
`git.exe` sitting in a repository is a repository that gets to choose
which Git runs. Every resolution is checked against the project root and
refused if the executable is inside it — the project is untrusted content
(`CLAUDE.md`), and an executable is the most untrusted content there is.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from app.logging_config import get_logger

logger = get_logger("coding.toolchain")

#: Per probe. A `--version` that has not answered in this long is a tool
#: that is broken, not a tool that is slow.
PROBE_TIMEOUT_SECONDS = 10.0

AVAILABLE = "available"
MISSING = "missing"
UNSUPPORTED = "unsupported"
REFUSED = "refused"

#: Where a tool comes from, which is the difference between "the machine
#: has this" and "this repository brought its own".
SOURCE_SYSTEM = "system"
SOURCE_PROJECT = "project"
SOURCE_BUNDLED = "bundled"


@dataclass(frozen=True)
class ToolSpec:
    key: str
    display: str
    executables: tuple            # tried in order
    version_args: tuple = ("--version",)
    minimum: tuple = ()           # (major, minor) or empty for "any"
    depends: tuple = ()           # what stops working without it


#: Everything §6 names. Formatter, linter, type checker, test runner and
#: build tool are *declared by the project*, so they are discovered
#: separately — see `project_tools()`.
TOOLS: tuple = (
    ToolSpec("git", "Git", ("git",), depends=(
        "History, diffs, worktree isolation and the safe-commit proposal.",
    )),
    ToolSpec("node", "Node.js", ("node",), minimum=(18, 0), depends=(
        "Running a JavaScript or TypeScript project, and its dev-server preview.",
    )),
    ToolSpec("npm", "npm", ("npm",), depends=(
        "Installing declared dependencies, and running package.json scripts.",
    )),
    ToolSpec("pnpm", "pnpm", ("pnpm",), depends=(
        "Projects with a pnpm-lock.yaml.",
    )),
    ToolSpec("yarn", "Yarn", ("yarn",), depends=(
        "Projects with a yarn.lock.",
    )),
    ToolSpec("python", "Python", ("python", "python3", "py"), minimum=(3, 9), depends=(
        "Running a Python project and its tests.",
    )),
    ToolSpec("pip", "pip", ("pip", "pip3"), depends=(
        "Installing declared Python dependencies.",
    )),
)


@dataclass
class ToolReport:
    key: str
    display: str
    state: str = MISSING
    version: str = ""
    source: str = SOURCE_SYSTEM
    #: How the executable was found — never the path itself. A full
    #: Windows path contains the account name, and this goes on screen and
    #: into diagnostics.
    found_via: str = ""
    detail: str = ""
    depends: List[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.state == AVAILABLE

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "display": self.display,
            "state": self.state,
            "version": self.version,
            "source": self.source,
            "found_via": self.found_via,
            "detail": self.detail,
            "depends": self.depends,
            "usable": self.usable,
        }


# --------------------------------------------------------------------------
# Bounded discovery
# --------------------------------------------------------------------------

def _standard_locations(executable: str) -> List[Path]:
    """A short, named list. Never a search.

    These are the places a tool is installed by its own installer. If a
    user put Node somewhere else and did not add it to PATH, JARVIS
    reports it missing rather than going to look — going to look is how a
    "diagnostic" becomes a disk scan of somebody's drive.
    """
    candidates: List[Path] = []
    if os.name == "nt":
        program_files = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
        local = os.environ.get("LOCALAPPDATA")
        for base in program_files:
            if not base:
                continue
            candidates += [
                Path(base) / "nodejs" / f"{executable}.cmd",
                Path(base) / "nodejs" / f"{executable}.exe",
                Path(base) / "Git" / "cmd" / f"{executable}.exe",
            ]
        if local:
            candidates.append(Path(local) / "Programs" / "Python" / f"{executable}.exe")
    else:
        for base in ("/usr/local/bin", "/usr/bin", "/opt/homebrew/bin"):
            candidates.append(Path(base) / executable)
    return candidates


def _resolve(executable: str, project_root: Optional[Path]) -> tuple:
    """Where this executable is, and how it was found.

    Returns `(path, found_via, refusal)`. A refusal is a *named* reason,
    never a silent None: "JARVIS refused to run the git.exe in your
    project folder" and "Git is not installed" call for entirely
    different responses from the user.
    """
    found = shutil.which(executable)
    if found:
        refusal = _impersonation_refusal(Path(found), project_root)
        if refusal:
            return None, "", refusal
        return Path(found), "PATH", ""

    for candidate in _standard_locations(executable):
        try:
            if not candidate.is_file():
                continue
        except OSError:
            continue
        refusal = _impersonation_refusal(candidate, project_root)
        if refusal:
            return None, "", refusal
        return candidate, "a standard install location", ""
    return None, "", ""


def _impersonation_refusal(candidate: Path, project_root: Optional[Path]) -> str:
    """Refuse an executable that lives inside the project being worked on.

    Windows' historical "current directory first" PATH behaviour means a
    `git.exe` committed to a repository can be the Git that runs. A
    repository does not get to choose which toolchain inspects it — the
    same rule that makes a project's declared npm script untrusted until
    its body is screened.
    """
    if project_root is None:
        return ""
    try:
        resolved = candidate.resolve()
        root = project_root.resolve()
    except (OSError, RuntimeError):
        return ""
    if resolved == root or root in resolved.parents:
        return (
            f"An executable named '{candidate.name}' is inside this project. JARVIS "
            "will not run a program a repository supplied, so this tool is treated "
            "as unavailable."
        )
    return ""


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

def _version_of(path: Path, args: tuple) -> tuple:
    """Ask the tool. Returns `(text, error)`; never raises."""
    from app.coding.runner import build_environment

    try:
        result = subprocess.run(  # noqa: S603 — argv list, shell=False, resolved path
            [str(path), *args],
            capture_output=True, text=True, shell=False,
            timeout=PROBE_TIMEOUT_SECONDS,
            env=build_environment(),
        )
    except subprocess.TimeoutExpired:
        return "", "It did not answer --version within 10 seconds."
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"It could not be run ({type(exc).__name__})."

    text = ((result.stdout or "") + " " + (result.stderr or "")).strip()
    if result.returncode != 0 and not text:
        return "", f"--version exited with code {result.returncode}."
    return (_without_paths(text.splitlines()[0])[:120] if text else ""), ""


def _without_paths(text: str) -> str:
    """Strip filesystem paths out of a version banner.

    `pip --version` answers "pip 24.0 from C:\\Users\\<name>\\AppData\\...",
    and this string goes on screen and into diagnostics. A full Windows
    path contains the account name — the same rule
    `app/launcher/process_tree.py` follows for exactly the same reason.
    """
    import re

    cleaned = re.split(r"\s+from\s+", text, maxsplit=1)[0]
    cleaned = re.sub(r"[A-Za-z]:\\[^\s)]+", "…", cleaned)
    cleaned = re.sub(r"(?<![\w.])/[^\s)]{2,}", "…", cleaned)
    return cleaned.strip()


def _numbers(text: str) -> tuple:
    import re

    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return ()
    return tuple(int(part) for part in match.groups() if part is not None)


def probe(spec: ToolSpec, project_root: Optional[Path] = None) -> ToolReport:
    """One tool, looked at and not changed."""
    report = ToolReport(key=spec.key, display=spec.display, depends=list(spec.depends))

    refusal = ""
    for executable in spec.executables:
        path, found_via, this_refusal = _resolve(executable, project_root)
        refusal = refusal or this_refusal
        if path is None:
            continue

        text, error = _version_of(path, spec.version_args)
        if error:
            report.state = UNSUPPORTED
            report.detail = error
            report.found_via = found_via
            return report

        report.version = text
        report.found_via = found_via
        numbers = _numbers(text)
        if spec.minimum and numbers and numbers[:len(spec.minimum)] < spec.minimum:
            report.state = UNSUPPORTED
            report.detail = (
                f"JARVIS needs {spec.display} "
                f"{'.'.join(str(n) for n in spec.minimum)} or newer."
            )
        else:
            report.state = AVAILABLE
        return report

    if refusal:
        report.state = REFUSED
        report.detail = refusal
        return report

    report.state = MISSING
    report.detail = f"{spec.display} was not found on PATH or in a standard location."
    return report


# --------------------------------------------------------------------------
# The project's own declarations
# --------------------------------------------------------------------------

#: What a declared script is *for*, so the report can say which function
#: stops working. Matched against the script's name, never its body — the
#: body is untrusted and is screened separately by `commands.py`.
_INTENTS = {
    "format": ("Formatter", ("format", "fmt", "prettier")),
    "lint": ("Linter", ("lint", "eslint", "ruff", "flake8")),
    "typecheck": ("Type checker", ("typecheck", "type-check", "tsc", "mypy")),
    "test": ("Test runner", ("test", "tests", "pytest", "vitest", "jest")),
    "build": ("Build tool", ("build", "compile", "bundle")),
}


def project_tools(root: Path) -> List[dict]:
    """The formatter, linter, type checker, test runner and build tool
    this project declares — read from the project, never guessed.

    A project that declares none of them reports none of them. Inventing a
    plausible default ("it is probably jest") would produce a report that
    is wrong in exactly the case where being wrong matters.
    """
    from app.coding import stacks

    detected = stacks.detect(root)
    declared = stacks.project_commands(detected) or {}

    rows: List[dict] = []
    for intent, (label, keywords) in _INTENTS.items():
        entry = declared.get(intent)
        name = ""
        if entry:
            argv = entry.get("argv") if isinstance(entry, dict) else None
            name = " ".join(str(a) for a in (argv or []))[:120]
        rows.append({
            "intent": intent,
            "display": label,
            "declared": bool(entry),
            "command": name,
            "detail": (
                f"Declared by the project as '{name}'." if entry else
                f"This project declares no {label.lower()}. JARVIS will not guess one, "
                f"so nothing that needs it can run."
            ),
            "keywords": list(keywords),
        })
    return rows


def virtual_environments(root: Path) -> List[dict]:
    """Project-local Python environments, found by looking at the two
    conventional names rather than by searching."""
    rows: List[dict] = []
    for name in (".venv", "venv"):
        candidate = root / name
        interpreter = (candidate / "Scripts" / "python.exe" if os.name == "nt"
                       else candidate / "bin" / "python")
        try:
            present = interpreter.is_file()
        except OSError:
            present = False
        if present:
            rows.append({
                "name": name,
                "state": AVAILABLE,
                "source": SOURCE_PROJECT,
                "detail": f"A Python environment in {name}/ — JARVIS uses it for this project.",
            })
    if not rows:
        rows.append({
            "name": "(none)",
            "state": MISSING,
            "source": SOURCE_PROJECT,
            "detail": "No .venv or venv folder. Python commands use the system interpreter.",
        })
    return rows


# --------------------------------------------------------------------------
# The whole picture
# --------------------------------------------------------------------------

def diagnose(root: Optional[Path] = None) -> dict:
    """Everything §6 asks for, in one read-only pass."""
    reports = [probe(spec, root) for spec in TOOLS]
    payload: Dict[str, object] = {
        "tools": [r.as_dict() for r in reports],
        "missing": [r.display for r in reports if r.state == MISSING],
        "unsupported": [r.display for r in reports if r.state == UNSUPPORTED],
        "refused": [r.display for r in reports if r.state == REFUSED],
        "nothing_was_installed": True,
        "nothing_was_changed": True,
    }
    payload["cannot_run"] = sorted({
        line for r in reports if not r.usable for line in r.depends
    })
    if root is not None:
        payload["project_tools"] = project_tools(root)
        payload["virtual_environments"] = virtual_environments(root)
    return payload


def blocked_reason(intent: str, root: Optional[Path] = None) -> str:
    """Why a validation step cannot run, or "" if it can.

    Used before a check is reported as passed. A formatter that is not
    installed did not agree the code is formatted; it did not look.
    """
    needed = {
        "format": ("node", "npm"),
        "lint": ("node", "npm"),
        "typecheck": ("node", "npm"),
        "test": (),
        "build": ("node", "npm"),
    }.get(intent, ())
    for key in needed:
        spec = next((s for s in TOOLS if s.key == key), None)
        if spec is None:
            continue
        report = probe(spec, root)
        if not report.usable:
            return (
                f"{report.display} is {report.state}, so the {intent} step did not run. "
                "This is not a passing result."
            )
    return ""
