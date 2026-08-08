"""Sanity checks for packaging/jarvis.spec, packaging/version_info.txt,
and the generated icon assets.

This cannot validate a real PyInstaller build — PyInstaller doesn't
cross-compile a Windows executable on Linux, and this project's own
rule (matching the user's explicit instruction) is not to try. These
tests catch the class of regression that's cheap to catch without a
real build: syntax errors, an icon path that no longer points at a
real file, a version string that drifted out of sync with
app/__init__.py. Real build validation happens on windows-latest CI —
see the packaging report.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "packaging" / "jarvis.spec"
VERSION_INFO_PATH = REPO_ROOT / "packaging" / "version_info.txt"
ICON_ICO_PATH = REPO_ROOT / "app" / "ui" / "static" / "icon.ico"
ICON_PNG_PATH = REPO_ROOT / "app" / "ui" / "static" / "icon.png"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# jarvis.spec
# ---------------------------------------------------------------------------

def test_spec_file_exists():
    assert SPEC_PATH.exists()


def test_spec_file_is_valid_python_syntax():
    ast.parse(_read(SPEC_PATH))  # raises SyntaxError on failure


def test_spec_builds_windowed_no_console():
    assert "console=False" in _read(SPEC_PATH)


def test_spec_references_the_real_icon_file():
    content = _read(SPEC_PATH)
    assert "icon.ico" in content
    assert ICON_ICO_PATH.exists()


def test_spec_references_the_real_version_info_file():
    content = _read(SPEC_PATH)
    assert "version_info.txt" in content
    assert VERSION_INFO_PATH.exists()


def test_spec_entry_point_is_run_jarvis():
    assert "run_jarvis.py" in _read(SPEC_PATH)


def test_spec_includes_required_hidden_imports():
    content = _read(SPEC_PATH)
    for expected in ("pyttsx3.drivers.sapi5", "comtypes", "keyring.backends.Windows"):
        assert expected in content


def test_spec_collects_all_for_packages_that_need_full_collection():
    """Regression guard for a real failure caught on windows-latest CI:
    a real frozen JARVIS.exe launched, stayed running, and never
    answered /health — consistent with an import failing silently
    inside the background uvicorn thread (app/launcher/server_runner.py)
    rather than crashing the process. The pre-existing, separate
    .github/workflows/windows-build.yml build job already needed
    `--collect-all pydantic_settings --collect-all anthropic
    --collect-all pyttsx3` for exactly this reason; this spec had only
    ever carried the narrower pyttsx3 driver hidden-import, which was
    not enough on its own."""
    tree = ast.parse(_read(SPEC_PATH))
    collected_packages = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "collect_all":
            arg = node.args[0]
            if isinstance(arg, ast.Constant):
                collected_packages.add(arg.value)
    if not collected_packages:
        # Loop form (`for _pkg in (...): collect_all(_pkg)`) — find the
        # iterated tuple instead of a literal argument at each call site.
        for node in ast.walk(tree):
            if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
                if any(isinstance(e, ast.Constant) and e.value == "anthropic" for e in node.iter.elts):
                    collected_packages.update(e.value for e in node.iter.elts if isinstance(e, ast.Constant))
    for expected in ("pydantic_settings", "anthropic", "pyttsx3", "webview", "pythonnet", "clr_loader"):
        assert expected in collected_packages, f"{expected} must go through collect_all(), not just hiddenimports"


def test_spec_bundles_templates_and_static():
    content = _read(SPEC_PATH)
    assert "app/ui/templates" in content
    assert "app/ui/static" in content


def test_spec_does_not_bundle_env_example():
    """The whole point of the onboarding flow: the packaged app must
    never ship or need a .env template. Checks the actual datas=[...]
    list specifically — the spec's own comment *mentioning*
    ".env.example" to explain the exclusion is legitimate documentation,
    not a bundling instruction, so a blunt substring check over the
    whole file would false-positive on that comment."""
    tree = ast.parse(_read(SPEC_PATH))
    datas_assignment = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "datas" for t in node.targets)
    )
    datas_source = ast.dump(datas_assignment)
    assert ".env.example" not in datas_source


# ---------------------------------------------------------------------------
# version_info.txt
# ---------------------------------------------------------------------------

def test_version_info_file_exists():
    assert VERSION_INFO_PATH.exists()


def test_version_info_is_valid_python_syntax():
    ast.parse(_read(VERSION_INFO_PATH))


def test_version_info_matches_app_version():
    from app import __version__
    content = _read(VERSION_INFO_PATH)
    assert __version__ in content


def test_version_info_has_no_fake_company_claim():
    """CLAUDE.md's packaging rules explicitly forbid fake company/legal
    info — the real repo owner (verifiable on GitHub) is used instead
    of an invented company name."""
    content = _read(VERSION_INFO_PATH)
    assert "Dado211207" in content
    for fake_sounding in ("Inc.", "LLC", "Corporation", "Ltd."):
        assert fake_sounding not in content


def test_version_info_does_not_claim_a_nonexistent_license_file():
    content = _read(VERSION_INFO_PATH)
    assert "LICENSE file" not in content or "No LICENSE file exists" in content


# ---------------------------------------------------------------------------
# Icon assets
# ---------------------------------------------------------------------------

def test_icon_ico_exists_and_has_multiple_sizes():
    from PIL import Image
    assert ICON_ICO_PATH.exists()
    with Image.open(ICON_ICO_PATH) as img:
        sizes = {s for s in img.info.get("sizes", [])}
    for required in ((16, 16), (32, 32), (48, 48), (256, 256)):
        assert required in sizes, f"icon.ico is missing the {required} size"


def test_icon_png_exists_and_is_square():
    from PIL import Image
    assert ICON_PNG_PATH.exists()
    with Image.open(ICON_PNG_PATH) as img:
        assert img.width == img.height
        assert img.width >= 256
