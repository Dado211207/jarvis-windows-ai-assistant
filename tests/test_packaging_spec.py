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


def _collected_packages() -> set:
    """Every package the spec puts through collect_all().

    Resolves module-level tuple constants, because the spec names its
    required and optional sets separately — a walk that only understood a
    literal loop iterator would silently find nothing and let every
    assertion below pass vacuously (see the guard test right after this).
    """
    tree = ast.parse(_read(SPEC_PATH))

    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Tuple, ast.List)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = [
                        e.value for e in node.value.elts if isinstance(e, ast.Constant)
                    ]

    def _resolve(expr):
        if isinstance(expr, (ast.Tuple, ast.List)):
            return [e.value for e in expr.elts if isinstance(e, ast.Constant)]
        if isinstance(expr, ast.Name):
            return constants.get(expr.id, [])
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            return _resolve(expr.left) + _resolve(expr.right)
        return []

    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "collect_all":
            arg = node.args[0]
            if isinstance(arg, ast.Constant):
                found.add(arg.value)
        if isinstance(node, ast.For):
            found.update(_resolve(node.iter))
    return found


def test_the_spec_walk_actually_finds_packages():
    """Guards every assertion below against passing vacuously if the
    spec's structure changes in a way this parser does not understand."""
    assert len(_collected_packages()) >= 6


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
    collected = _collected_packages()
    for expected in ("pydantic_settings", "anthropic", "pyttsx3", "webview", "pythonnet", "clr_loader"):
        assert expected in collected, f"{expected} must go through collect_all(), not just hiddenimports"


def test_the_speech_engine_is_bundled():
    """The reported defect: the installed app said "Speech runtime — Not
    ready" permanently, because faster-whisper was deliberately excluded
    from the installer. No action available to a user could fix that.

    The engine is code and ships with the app. The model is data and is
    still downloaded on request, with its licence, size and checksum
    shown first — see test_the_model_is_still_not_bundled below."""
    collected = _collected_packages()
    assert "faster_whisper" in collected
    assert "ctranslate2" in collected, "faster-whisper's compiled backend carries its own DLLs"


def test_a_missing_required_package_fails_the_build_loudly():
    """A JARVIS.exe missing its speech engine is broken in a way that
    only shows up at runtime, on a user's machine. The spec must refuse
    to produce one rather than warn into a build log."""
    source = _read(SPEC_PATH)
    assert "_REQUIRED_PACKAGES" in source
    assert "raise SystemExit" in source


def test_the_model_is_still_not_bundled():
    """Bundling a speech model would add hundreds of megabytes to the
    installer for something most users never turn on, and CLAUDE.md
    requires its licence, size and checksum to be shown before it is
    fetched."""
    source = _read(SPEC_PATH)
    assert "faster-whisper-tiny" not in source
    assert "model.bin" not in source


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


def test_the_licence_texts_are_bundled():
    """The obligation is to the person holding the binary, so the licence
    travels with the product rather than with a link to it."""
    source = _read(SPEC_PATH)

    assert 'docs" / "licences"' in source or '"docs/licences"' in source


def test_the_pronunciation_lexicon_is_bundled():
    """PyInstaller collects package data for third-party packages via
    collect_all, but not for the application's own modules. Without an
    explicit datas entry the installed app has a voice that cannot
    pronounce anything and spells every word instead."""
    source = _read(SPEC_PATH)

    assert "kokoro" in source and "data" in source
    assert "app/voice/kokoro/data" in source
