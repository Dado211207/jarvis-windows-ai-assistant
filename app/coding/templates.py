"""Bundled starter templates.

**Nothing here touches the network.** `npm create vite@latest` and its
relatives download and execute a scaffolder, which is a package
installation wearing a friendlier name — and doing it automatically, as
part of "make me a new project", would install and run third-party code
without the disclosure that installing a package requires everywhere else
in this feature.

So the templates are a handful of files written from constants in this
module. They produce a project that opens, has a sensible structure and
is honest about needing `npm install` before it will run — which stays a
separate, approved action.

The files are deliberately minimal. A starter that ships fifty files
nobody reads is not a favour; it is a large diff on day one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from app.coding import projects
from app.coding.workspace import WorkspaceViolation, canonical_root, resolve
from app.logging_config import get_logger

logger = get_logger("coding.templates")

# A project directory name, not a path. Anything that could traverse or
# name a device is refused rather than cleaned up.
_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}$")
_RESERVED = {
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    description: str
    stack: str
    files: Dict[str, str]
    needs_install: bool
    install_hint: str
    init_git: bool = True

    def describe(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "stack": self.stack,
            "files": sorted(self.files),
            "file_count": len(self.files),
            "commands_to_be_run": [] if not self.needs_install else [],
            "dependencies_installed_now": False,
            "network_use": "none",
            "install_hint": self.install_hint,
            "git_initialised": self.init_git,
        }


_STATIC_INDEX = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{name}</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header>
    <h1>{name}</h1>
  </header>
  <main>
    <p>This page was created by JARVIS. Edit <code>index.html</code> to change it.</p>
  </main>
  <script src="main.js"></script>
</body>
</html>
"""

_STATIC_CSS = """:root {
  color-scheme: light dark;
  --ink: #1a1a1a;
  --paper: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root { --ink: #f2f2f2; --paper: #161616; }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 2rem;
  font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
  color: var(--ink);
  background: var(--paper);
  max-width: 70ch;
}
h1 { font-size: clamp(1.6rem, 4vw, 2.4rem); }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
}
"""

_STATIC_JS = """// Edit this file to add behaviour.
document.addEventListener("DOMContentLoaded", () => {
  console.log("Ready.");
});
"""

_VITE_PACKAGE = {
    "name": "PLACEHOLDER",
    "private": True,
    "version": "0.0.0",
    "type": "module",
    "scripts": {
        "dev": "vite",
        "build": "tsc && vite build",
        "preview": "vite preview",
        "typecheck": "tsc --noEmit",
    },
    "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1"},
    "devDependencies": {
        "@types/react": "^18.3.3",
        "@types/react-dom": "^18.3.0",
        "@vitejs/plugin-react": "^4.3.1",
        "typescript": "^5.5.3",
        "vite": "^5.3.4",
    },
}

_VITE_CONFIG = """import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Loopback only. JARVIS starts previews on 127.0.0.1 and will not
    // expose this project on a network.
    host: "127.0.0.1",
  },
});
"""

_VITE_TSCONFIG = {
    "compilerOptions": {
        "target": "ES2020",
        "lib": ["ES2020", "DOM", "DOM.Iterable"],
        "module": "ESNext",
        "moduleResolution": "bundler",
        "jsx": "react-jsx",
        "strict": True,
        "noEmit": True,
        "skipLibCheck": True,
        "esModuleInterop": True,
    },
    "include": ["src"],
}

_VITE_INDEX = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{name}</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
"""

_VITE_MAIN = """import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
"""

_VITE_APP = """export default function App() {
  return (
    <main>
      <h1>{name}</h1>
      <p>Edit <code>src/App.tsx</code> to change this page.</p>
    </main>
  );
}
"""

_VITE_CSS = """:root { color-scheme: light dark; }
body {
  margin: 0;
  padding: 2rem;
  font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
}
"""

_PY_MAIN = '''"""{name}."""


def greet(who: str = "world") -> str:
    return f"Hello, {{who}}!"


if __name__ == "__main__":
    print(greet())
'''

_PY_TEST = '''from main import greet


def test_greet_defaults_to_world():
    assert greet() == "Hello, world!"


def test_greet_uses_the_name_given():
    assert greet("JARVIS") == "Hello, JARVIS!"
'''

_PY_PYPROJECT = """[project]
name = "{slug}"
version = "0.1.0"
requires-python = ">=3.9"

[tool.pytest.ini_options]
testpaths = ["."]
"""

_FASTAPI_MAIN = '''"""{name} — a small FastAPI application.

Run it on 127.0.0.1. Binding to every network interface instead would
publish this application to every network this machine is attached to,
which is rarely what a project in development wants.
"""

from fastapi import FastAPI

app = FastAPI(title="{name}")


@app.get("/health")
def health() -> dict:
    return {{"status": "ok"}}


@app.get("/")
def index() -> dict:
    return {{"message": "Hello from {name}."}}
'''

_FASTAPI_TEST = '''from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_health_reports_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
'''

_GITIGNORE_NODE = """node_modules/
dist/
.env
.env.*
*.log
"""

_GITIGNORE_PY = """__pycache__/
*.pyc
.venv/
venv/
.env
.env.*
.pytest_cache/
"""


def _templates() -> Dict[str, Template]:
    return {
        "static": Template(
            key="static",
            title="Static site",
            description="A single HTML page with CSS and JavaScript. No build step, no dependencies.",
            stack="Static HTML/CSS/JavaScript",
            files={"index.html": _STATIC_INDEX, "styles.css": _STATIC_CSS,
                   "main.js": _STATIC_JS, ".gitignore": _GITIGNORE_NODE},
            needs_install=False,
            install_hint="Nothing to install — open index.html.",
        ),
        "react-vite-ts": Template(
            key="react-vite-ts",
            title="React + Vite + TypeScript",
            description="A minimal React application with TypeScript and Vite.",
            stack="React + Vite + TypeScript",
            files={
                "package.json": "", "vite.config.ts": _VITE_CONFIG,
                "tsconfig.json": "", "index.html": _VITE_INDEX,
                "src/main.tsx": _VITE_MAIN, "src/App.tsx": _VITE_APP,
                "src/index.css": _VITE_CSS, ".gitignore": _GITIGNORE_NODE,
            },
            needs_install=True,
            install_hint="Dependencies are listed but not installed. Ask JARVIS to run "
                         "the install and approve it when prompted.",
        ),
        "python": Template(
            key="python",
            title="Python",
            description="A Python module with a pytest test beside it.",
            stack="Python",
            files={"main.py": _PY_MAIN, "test_main.py": _PY_TEST,
                   "pyproject.toml": "", ".gitignore": _GITIGNORE_PY},
            needs_install=False,
            install_hint="pytest is needed to run the test; JARVIS will ask before installing anything.",
        ),
        "fastapi": Template(
            key="fastapi",
            title="FastAPI",
            description="A small FastAPI service bound to loopback, with a health check and a test.",
            stack="Python + FastAPI",
            files={"main.py": _FASTAPI_MAIN, "test_main.py": _FASTAPI_TEST,
                   "requirements.txt": "fastapi\nuvicorn\nhttpx\npytest\n",
                   "pyproject.toml": "", ".gitignore": _GITIGNORE_PY},
            needs_install=True,
            install_hint="fastapi and uvicorn are listed but not installed.",
        ),
    }


def describe_all() -> List[dict]:
    return [t.describe() for t in _templates().values()]


def validate_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not _VALID_NAME.match(cleaned):
        raise WorkspaceViolation(
            "A project name may use letters, numbers, spaces, dots, hyphens and "
            "underscores, and must start with a letter or number."
        )
    if cleaned.split(".")[0].lower() in _RESERVED:
        raise WorkspaceViolation(f"'{cleaned}' is a reserved Windows device name.")
    if ".." in cleaned:
        raise WorkspaceViolation("A project name may not contain '..'.")
    # Windows silently strips a trailing dot or space from a directory
    # name, so "my-site." asks for one folder and gets another. The user
    # is then looking at a project whose name is not what they typed, and
    # "my-site." and "my-site" collide with no explanation. Refusing is
    # the only outcome that matches what the user sees.
    if cleaned.endswith((".", " ")):
        raise WorkspaceViolation(
            "A project name may not end with a dot or a space — Windows removes "
            "them from the folder name, so the project would not be called what "
            "you typed."
        )
    return cleaned


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "project"


def _render(template: Template, name: str) -> Dict[str, str]:
    slug = _slug(name)
    files = dict(template.files)

    if template.key == "react-vite-ts":
        package = dict(_VITE_PACKAGE)
        package["name"] = slug
        files["package.json"] = json.dumps(package, indent=2) + "\n"
        files["tsconfig.json"] = json.dumps(_VITE_TSCONFIG, indent=2) + "\n"
        files["index.html"] = _VITE_INDEX.format(name=name)
        files["src/App.tsx"] = _VITE_APP.replace("{name}", name)
    elif template.key == "static":
        files["index.html"] = _STATIC_INDEX.format(name=name)
    elif template.key in ("python", "fastapi"):
        files["pyproject.toml"] = _PY_PYPROJECT.format(slug=slug)
        if template.key == "python":
            files["main.py"] = _PY_MAIN.format(name=name)
        else:
            files["main.py"] = _FASTAPI_MAIN.format(name=name)

    return files


def create(parent_path: str, name: str, template_key: str) -> projects.Project:
    """Create a project inside a folder the user chose, and register it.

    The parent must already exist and be a real directory the user
    selected — this never creates a path halfway up a tree nobody asked
    for.
    """
    clean_name = validate_name(name)
    template = _templates().get(template_key)
    if template is None:
        raise WorkspaceViolation(f"'{template_key}' is not a template JARVIS ships.")

    parent = canonical_root(parent_path)
    destination = parent / clean_name

    # The destination is checked against the *parent* as its root, which
    # is what stops a name that escapes despite passing the name rules.
    contained = resolve(parent, clean_name)
    if contained.absolute != destination:
        raise WorkspaceViolation("That project name does not resolve inside the folder you chose.")

    if destination.exists():
        raise WorkspaceViolation(
            f"'{clean_name}' already exists in that folder. JARVIS will not write into it."
        )

    files = _render(template, clean_name)
    destination.mkdir(parents=True, exist_ok=False)
    for relative, content in files.items():
        target = resolve(destination, relative)
        target.absolute.parent.mkdir(parents=True, exist_ok=True)
        target.absolute.write_text(content, encoding="utf-8", newline="\n")

    if template.init_git:
        _init_git(destination)

    logger.info("Coding project scaffolded from the '%s' template.", template_key)
    return projects.add(str(destination), clean_name)


def _init_git(destination: Path) -> None:
    """`git init` only. No remote, no commit, no config change."""
    import subprocess

    from app.coding.runner import build_environment

    try:
        subprocess.run(  # noqa: S603 — argv list, shell=False
            ["git", "init", "-q"],
            cwd=str(destination), env=build_environment(),
            capture_output=True, timeout=30, shell=False, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        logger.info("git init did not run for the new project; continuing without it.")
