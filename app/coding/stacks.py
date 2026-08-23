"""Working out what a project *is*, from evidence rather than guesswork.

Two rules govern this module:

**Evidence, not assumption.** A stack is claimed only when a file that
proves it is present. "There is a `vite.config.ts`" is evidence. "The
folder is called `frontend`" is not.

**Never invent a command the project already defines.** If `package.json`
has a `test` script, the test command is `npm run test` — not `jest`,
not `vitest`, not whatever this module would have picked. Guessing a
command when the project has stated one is how a coding agent runs the
wrong thing and reports the wrong result. Where a project declares
nothing, this module says so and proposes nothing.

The package manager is read from the **lockfile**, because that is the
fact. Running `npm install` in a pnpm project creates a second lockfile
and a subtly different dependency tree, and the person who has to
untangle that is the user.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# Lockfile -> package manager. Order matters only for reporting; a project
# with two lockfiles is reported as ambiguous rather than resolved by
# preference, because picking one silently is the failure mode.
_NODE_LOCKFILES = {
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "package-lock.json": "npm",
    "bun.lockb": "bun",
}

_PYTHON_MARKERS = (
    "pyproject.toml", "requirements.txt", "requirements-dev.txt",
    "setup.py", "setup.cfg", "Pipfile", "poetry.lock",
)

_STATIC_ENTRIES = ("index.html", "index.htm")


@dataclass
class DetectedStack:
    """What the evidence supports, and what the evidence was."""

    kinds: List[str] = field(default_factory=list)
    package_manager: Optional[str] = None
    package_manager_ambiguous: List[str] = field(default_factory=list)
    node_scripts: Dict[str, str] = field(default_factory=dict)
    python_test_runner: Optional[str] = None
    python_formatter: Optional[str] = None
    python_type_checker: Optional[str] = None
    virtualenv: Optional[str] = None
    evidence: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if not self.kinds:
            return "Unrecognised"
        return " + ".join(self.kinds)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "kinds": self.kinds,
            "package_manager": self.package_manager,
            "package_manager_ambiguous": self.package_manager_ambiguous,
            "node_scripts": self.node_scripts,
            "python_test_runner": self.python_test_runner,
            "python_formatter": self.python_formatter,
            "python_type_checker": self.python_type_checker,
            "virtualenv": self.virtualenv,
            "evidence": self.evidence,
            "notes": self.notes,
        }


def _read_json(path: Path) -> Optional[dict]:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _read_text(path: Path, limit: int = 512 * 1024) -> str:
    try:
        if path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _detect_node(root: Path, stack: DetectedStack) -> None:
    package_json = root / "package.json"
    if not package_json.is_file():
        return

    data = _read_json(package_json)
    if data is None:
        stack.notes.append("package.json is present but could not be parsed.")
        return

    stack.evidence.append("package.json")

    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        stack.node_scripts = {
            str(k): str(v) for k, v in scripts.items() if isinstance(v, str)
        }

    deps: Dict[str, str] = {}
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        value = data.get(section)
        if isinstance(value, dict):
            deps.update({str(k): str(v) for k, v in value.items() if isinstance(v, str)})

    found = [name for name, manager in _NODE_LOCKFILES.items() if (root / name).is_file()]
    if len(found) == 1:
        stack.package_manager = _NODE_LOCKFILES[found[0]]
        stack.evidence.append(found[0])
    elif len(found) > 1:
        stack.package_manager_ambiguous = [_NODE_LOCKFILES[name] for name in found]
        stack.evidence.extend(found)
        stack.notes.append(
            "More than one lockfile is present, so the package manager is ambiguous. "
            "JARVIS will not choose one for you — running the wrong installer here "
            "creates a second dependency tree."
        )
    else:
        pm_field = data.get("packageManager")
        if isinstance(pm_field, str) and "@" in pm_field:
            stack.package_manager = pm_field.split("@", 1)[0]
            stack.evidence.append('package.json "packageManager"')
        else:
            stack.notes.append(
                "No lockfile found. Dependencies have probably never been installed here."
            )

    if "react" in deps:
        stack.kinds.append("React")
        stack.evidence.append("react dependency")
    if "vite" in deps or any((root / f"vite.config{ext}").is_file() for ext in (".ts", ".js", ".mjs", ".mts")):
        stack.kinds.append("Vite")
        for ext in (".ts", ".js", ".mjs", ".mts"):
            if (root / f"vite.config{ext}").is_file():
                stack.evidence.append(f"vite.config{ext}")
                break
    if "next" in deps:
        stack.kinds.append("Next.js")
    if not any(k in stack.kinds for k in ("React", "Vite", "Next.js")):
        stack.kinds.append("Node")


def _detect_typescript(root: Path, stack: DetectedStack) -> None:
    if (root / "tsconfig.json").is_file():
        stack.kinds.append("TypeScript")
        stack.evidence.append("tsconfig.json")


def _detect_python(root: Path, stack: DetectedStack) -> None:
    markers = [m for m in _PYTHON_MARKERS if (root / m).is_file()]
    if not markers:
        return

    stack.kinds.append("Python")
    stack.evidence.extend(markers)

    pyproject_text = _read_text(root / "pyproject.toml") if (root / "pyproject.toml").is_file() else ""
    requirements_text = "\n".join(
        _read_text(root / name)
        for name in ("requirements.txt", "requirements-dev.txt")
        if (root / name).is_file()
    )
    combined = f"{pyproject_text}\n{requirements_text}".lower()

    if (root / "pytest.ini").is_file() or "[tool.pytest" in combined or "pytest" in combined:
        stack.python_test_runner = "pytest"
    elif (root / "tox.ini").is_file():
        stack.python_test_runner = "tox"

    if "ruff" in combined:
        stack.python_formatter = "ruff"
    elif "black" in combined:
        stack.python_formatter = "black"

    if "mypy" in combined:
        stack.python_type_checker = "mypy"
    elif "pyright" in combined:
        stack.python_type_checker = "pyright"

    for name in (".venv", "venv", "env"):
        candidate = root / name
        if (candidate / "pyvenv.cfg").is_file():
            stack.virtualenv = name
            stack.evidence.append(f"{name}/pyvenv.cfg")
            break
    if stack.virtualenv is None:
        stack.notes.append(
            "No virtual environment found in the project. JARVIS never installs "
            "Python packages globally, so an install here would need one created first."
        )

    # FastAPI is a Python *framework*, evidenced either by the dependency or
    # by an actual import. Both are checked; a dependency that is never
    # imported is still evidence the project intends to use it.
    if "fastapi" in combined:
        stack.kinds.append("FastAPI")
        stack.evidence.append("fastapi dependency")
    else:
        for candidate in ("main.py", "app.py", "api.py", "server.py"):
            path = root / candidate
            if path.is_file() and re.search(r"^\s*from\s+fastapi\b|^\s*import\s+fastapi\b",
                                            _read_text(path), re.MULTILINE):
                stack.kinds.append("FastAPI")
                stack.evidence.append(f"fastapi import in {candidate}")
                break


def _detect_static(root: Path, stack: DetectedStack) -> None:
    if any(k in stack.kinds for k in ("React", "Vite", "Next.js", "Node")):
        return
    for entry in _STATIC_ENTRIES:
        if (root / entry).is_file():
            stack.kinds.append("Static site")
            stack.evidence.append(entry)
            return


def detect(root: Path) -> DetectedStack:
    """Everything this project's own files say about itself."""
    stack = DetectedStack()
    try:
        if not root.is_dir():
            stack.notes.append("The project folder is not available.")
            return stack
    except OSError:
        stack.notes.append("The project folder could not be read.")
        return stack

    _detect_node(root, stack)
    _detect_typescript(root, stack)
    _detect_python(root, stack)
    _detect_static(root, stack)

    if len([k for k in stack.kinds if k in ("React", "Vite", "Node", "Next.js")]) and "Python" in stack.kinds:
        stack.notes.append("Mixed frontend/backend project.")

    # De-duplicate while preserving order.
    seen = set()
    stack.kinds = [k for k in stack.kinds if not (k in seen or seen.add(k))]
    seen_ev = set()
    stack.evidence = [e for e in stack.evidence if not (e in seen_ev or seen_ev.add(e))]
    return stack


# --------------------------------------------------------------------------
# Proposing commands — only ever from what the project declared.
# --------------------------------------------------------------------------

_SCRIPT_INTENTS = {
    "test": ("test", "tests", "test:unit", "vitest", "jest"),
    "lint": ("lint", "eslint", "lint:js"),
    "format": ("format", "fmt", "prettier"),
    "typecheck": ("typecheck", "type-check", "tsc", "types"),
    "build": ("build", "compile"),
    "dev": ("dev", "start", "serve", "preview"),
}


def _run_prefix(manager: Optional[str]) -> List[str]:
    if manager == "pnpm":
        return ["pnpm", "run"]
    if manager == "yarn":
        return ["yarn", "run"]
    if manager == "bun":
        return ["bun", "run"]
    return ["npm", "run"]


def project_commands(stack: DetectedStack) -> Dict[str, dict]:
    """The commands this project actually supports, keyed by intent.

    Each entry carries the argv **and** the evidence for it, so the UI can
    show why a command was proposed rather than asking the user to trust
    that it was right.

    An intent with no evidence is absent. It is never filled in with a
    plausible default.
    """
    commands: Dict[str, dict] = {}

    if stack.node_scripts and not stack.package_manager_ambiguous:
        prefix = _run_prefix(stack.package_manager)
        for intent, candidates in _SCRIPT_INTENTS.items():
            for name in candidates:
                if name in stack.node_scripts:
                    commands[intent] = {
                        "argv": prefix + [name],
                        "source": f'package.json scripts."{name}"',
                        "declared": stack.node_scripts[name],
                    }
                    break

    if "Python" in stack.kinds:
        if stack.python_test_runner == "pytest" and "test" not in commands:
            commands["test"] = {
                "argv": ["python", "-m", "pytest", "-q"],
                "source": "pytest configuration found in the project",
                "declared": "",
            }
        if stack.python_formatter == "ruff" and "lint" not in commands:
            commands["lint"] = {
                "argv": ["python", "-m", "ruff", "check", "."],
                "source": "ruff listed in project dependencies",
                "declared": "",
            }
        elif stack.python_formatter == "black" and "format" not in commands:
            commands["format"] = {
                "argv": ["python", "-m", "black", "--check", "."],
                "source": "black listed in project dependencies",
                "declared": "",
            }
        if stack.python_type_checker == "mypy" and "typecheck" not in commands:
            commands["typecheck"] = {
                "argv": ["python", "-m", "mypy", "."],
                "source": "mypy listed in project dependencies",
                "declared": "",
            }

    return commands


def missing_intents(stack: DetectedStack) -> List[str]:
    """Intents a project of this kind would normally have but does not.

    Reported so the UI can say "this project declares no test command"
    instead of JARVIS inventing one and reporting its result as the
    project's own.
    """
    available = set(project_commands(stack))
    expected = {"test", "lint", "build"} if stack.kinds else set()
    if "Static site" in stack.kinds:
        expected = set()
    return sorted(expected - available)
