"""Guards that the installed app never tells its user to do something
only a developer running from source could do.

Someone using the packaged JARVIS.exe has no repository, no `.env`, no
`.bat` files, no terminal, and no Python — telling them to "copy
.env.example to .env" or "run START_JARVIS_API.bat" is not a small
wording problem, it's an instruction they cannot follow at all. This is
exactly the class of thing that survives a feature-complete build
because everything still *works* for the developer who wrote it.

Real finding, not hypothetical: an audit for the release-candidate pass
found six such strings live in shipped UI (the Help page's whole
"API Key Setup" section, the Voice page's TTS note) plus four in
user-visible API/tool responses — all written before the app stored its
key in the Windows credential store and had a Setup page to do it from.

Scope note: this checks *user-facing* surfaces only. README.md,
CLAUDE.md, docs/, and requirements files legitimately talk about `.env`
and pip — they're for developers, and they are deliberately not scanned.
"""

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "app" / "ui" / "templates"

# Patterns a packaged-app user cannot act on. Deliberately narrow and
# literal — this is a guard against regressions in prose, not a linter.
FORBIDDEN_PATTERNS = [
    (r"\.env\b", "tells the user about a .env file"),
    (r"\.bat\b", "tells the user to run a .bat file"),
    (r"pip install", "tells the user to run pip"),
    (r"python -m ", "tells the user to run a python command"),
    (r"requirements[-\w]*\.txt", "tells the user about a requirements file"),
]


def _template_files():
    return sorted(TEMPLATES_DIR.glob("*.html"))


def test_templates_directory_is_found():
    """Guards the guard: if the templates move, every test below would
    silently pass by iterating an empty list."""
    assert _template_files(), f"no templates found under {TEMPLATES_DIR}"


@pytest.mark.parametrize("pattern,description", FORBIDDEN_PATTERNS)
def test_no_developer_instructions_in_templates(pattern, description):
    offenders = []
    for path in _template_files():
        content = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(content.splitlines(), start=1):
            if re.search(pattern, line, flags=re.IGNORECASE):
                offenders.append(f"{path.name}:{line_number}: {line.strip()}")
    assert not offenders, (
        f"A user-facing template {description}, which a packaged-app user "
        f"cannot do:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# The same rule for strings that reach the user through the API, not a
# template — error envelopes, tool results, and availability messages.
# ---------------------------------------------------------------------------

USER_FACING_MESSAGE_SOURCES = [
    REPO_ROOT / "app" / "core" / "errors.py",
    REPO_ROOT / "app" / "core" / "brain.py",
    REPO_ROOT / "app" / "voice" / "stt.py",
    REPO_ROOT / "app" / "api" / "routes.py",
]


def _user_facing_literals(source: str):
    """Every string literal in *source* except docstrings.

    Docstrings are excluded deliberately, and this is the whole reason
    this uses AST rather than a regex over the raw text: these modules'
    own docstrings legitimately explain *why* something is deliberately
    NOT done ("no .env edit, no restart", "deliberately NOT in
    requirements.txt"), and that prose never reaches a user. A first
    regex-based draft of this check flagged exactly those docstrings —
    the same comment-vs-code false-positive class already documented in
    tests/test_clean_install_script.py::_code_only(), rediscovered here
    and fixed properly instead of by special-casing individual words."""
    tree = ast.parse(source)

    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstring_nodes.add(id(body[0].value))

    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstring_nodes
    ]


@pytest.mark.parametrize("path", USER_FACING_MESSAGE_SOURCES, ids=lambda p: p.name)
def test_no_developer_instructions_in_user_facing_messages(path):
    offenders = []
    for literal in _user_facing_literals(path.read_text(encoding="utf-8")):
        # Only prose actually shown to someone — a bare env-var name in a
        # settings key or an f-string fragment isn't an instruction.
        if len(literal) < 25:
            continue
        for pattern, description in FORBIDDEN_PATTERNS:
            if re.search(pattern, literal, flags=re.IGNORECASE):
                offenders.append(f"{path.name}: {description}: {literal!r}")
    assert not offenders, "\n".join(offenders)
