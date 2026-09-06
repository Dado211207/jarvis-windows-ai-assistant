"""Every `browser`-marked test file must be named in the browser CI job.

`pytest.ini` deselects the `browser` marker from the default run, and
`.github/workflows/ci.yml`'s browser job takes an **explicit file list**
rather than collecting the whole tree. A file that is marked and not
listed therefore runs in no CI job at all — it is deselected in one and
never collected in the other, and nothing goes red to say so.

That has now happened twice: `tests/test_coding_preview.py`'s three
browser tests, and `tests/test_runtime_ordering.py` on its first push —
the regression cover for three ordering defects a review had just found,
silently absent from the run that was supposed to prove the fix.

A third time is preventable, so this is the guard. It fails when a file
is added and not listed; adding the file to the workflow is the fix.
"""

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
TESTS_DIR = REPO_ROOT / "tests"

#: Files whose browser tests are deliberately run somewhere other than the
#: browser job. Empty on purpose: an exemption here is a claim that some
#: other job covers the file, and that claim should be written down next
#: to the name if it is ever true.
EXEMPT: dict = {}


def _browser_job_file_list() -> str:
    """The `pytest -m browser ...` invocation, as written in the workflow."""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    marker = "pytest -m browser -v"
    if marker not in text:
        # Not an assert: this is the guard failing, not the thing it
        # guards. If the invocation is rewritten, this file has to be
        # rewritten with it — silently passing would be the worst
        # outcome, since it would report coverage it never checked.
        pytest.fail(
            "the browser job no longer runs `pytest -m browser -v`; "
            "update tests/test_ci_browser_job.py to match the new "
            "invocation in .github/workflows/ci.yml"
        )
    start = text.index(marker)
    # The run: block is a folded scalar; it ends at the next key at a
    # shallower indent ("env:" today).
    end = text.index("\n        env:", start)
    return text[start:end]


def _is_browser_marker(node) -> bool:
    """`pytest.mark.browser`, as an expression."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "browser"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
    )


def _declares_browser_tests(source: str) -> bool:
    """Both forms in use: a module-level `pytestmark = pytest.mark.browser`
    and a per-test `@pytest.mark.browser`.

    Parsed rather than grepped. The first version of this searched the
    text for `pytest.mark.browser` and flagged *this file*, which only
    mentions the marker inside a docstring and a comment. A guard that
    cannot tell a mention from a use would eventually be silenced by
    somebody adding it to an exemption list, and then it would be
    guarding nothing.
    """
    tree = ast.parse(source)

    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            if any(_is_browser_marker(v) for v in values):
                return True

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if _is_browser_marker(target):
                    return True
    return False


def _files_declaring_browser_tests() -> set:
    found = set()
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if _declares_browser_tests(path.read_text(encoding="utf-8")):
            found.add(path.name)
    return found


def test_every_browser_marked_file_is_named_in_the_ci_browser_job():
    listed = _browser_job_file_list()
    missing = sorted(
        name for name in _files_declaring_browser_tests()
        if name not in EXEMPT and f"tests/{name}" not in listed
    )
    assert not missing, (
        "these files carry `browser` tests that run in NO CI job — the "
        "default run deselects the marker and the browser job's explicit "
        "file list does not name them. Add them to the `pytest -m browser` "
        f"invocation in .github/workflows/ci.yml: {missing}"
    )


def test_the_job_does_not_name_a_file_that_no_longer_exists():
    """The mirror of the rule above. A stale name is not a hard failure in
    pytest — it is an error, which turns the job red for a reason that
    reads nothing like "somebody renamed a file"."""
    listed = _browser_job_file_list()
    named = set(re.findall(r"tests/(test_[A-Za-z0-9_]+\.py)", listed))
    gone = sorted(name for name in named if not (TESTS_DIR / name).exists())
    assert not gone, f"the browser job names files that do not exist: {gone}"
