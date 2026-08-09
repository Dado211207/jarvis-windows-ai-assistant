"""No GPL component reaches the distributed product.

The owner's constraint is about what actually ships, so this checks the
things that actually ship: the requirement files the installer builds
from, the PyInstaller spec that decides what goes in the executable, the
source that could import something at runtime, and the URLs anything
might download from. Declared package metadata alone would not catch a
DLL bundled by a wheel, a model fetched at runtime, or an import added
without a requirement line.

Where a real installed tree is available (in CI, after the build) the
same policy is applied to it file by file. Where it is not, that is said
rather than skipped silently — see
test_the_packaged_tree_check_reports_when_it_cannot_run.
"""

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"

# Distribution names that are copyleft, or that exist to drive something
# copyleft. Each is here because it was considered for this product and
# rejected — see app/voice/kokoro/assets.py and g2p.py for the reasoning.
FORBIDDEN_PACKAGES = {
    "piper-tts", "piper_tts",
    "espeakng-loader", "espeakng_loader",
    "phonemizer-fork", "phonemizer_fork", "phonemizer",
    "espeak", "espeak-ng", "espeakng",
    "cmudict",          # the PyPI wrapper is GPL-3.0-or-later
    "pystray",          # LGPL-3.0
    "unidecode",        # GPL
    "pyqt5", "pyqt6",   # GPL unless separately licensed
}

# Module names that would mean one of the above is being imported at
# runtime, whatever the requirement files say.
FORBIDDEN_IMPORTS = {
    "piper", "piper_tts", "espeakng_loader", "phonemizer", "espeak",
    "cmudict", "pystray", "unidecode",
}

FORBIDDEN_BINARIES = ("espeak", "libespeak", "piper", "libpiper")

REQUIREMENT_FILES = (
    "requirements.txt",
    "requirements-windows.txt",
    "requirements-voice.txt",
)


def _requirement_lines():
    for name in REQUIREMENT_FILES:
        path = REPO_ROOT / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                yield name, line


def _python_sources():
    return sorted(APP_DIR.rglob("*.py")) + sorted((REPO_ROOT / "scripts").rglob("*.py"))


# ---------------------------------------------------------------------------
# What the installer is built from
# ---------------------------------------------------------------------------

def test_the_requirement_files_are_actually_being_read():
    """Guards every assertion below from passing vacuously."""
    lines = list(_requirement_lines())

    assert len(lines) > 10
    assert any("fastapi" in line for _, line in lines)


def test_no_forbidden_package_is_required():
    offenders = []
    for source, line in _requirement_lines():
        name = re.split(r"[<>=!\[; ]", line, 1)[0].strip().lower()
        if name in FORBIDDEN_PACKAGES:
            offenders.append(f"{source}: {line}")

    assert offenders == [], f"copyleft or GPL-driving package required: {offenders}"


def test_no_misaki_extra_is_installed():
    """misaki itself is Apache-2.0 and can run without espeak. Its [en]
    extra is what pulls espeakng-loader and phonemizer-fork, so the
    objection is to the extra, not to the package."""
    for source, line in _requirement_lines():
        if "misaki" in line.lower():
            assert "[" not in line, (
                f"{source}: {line} — the misaki extras pull espeak-ng tooling; "
                "install only the permissive minimum"
            )


# ---------------------------------------------------------------------------
# What the executable is built from
# ---------------------------------------------------------------------------

def test_the_pyinstaller_spec_collects_nothing_forbidden():
    spec = (REPO_ROOT / "packaging" / "jarvis.spec").read_text(encoding="utf-8")
    tree = ast.parse(spec)

    named = {
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    offenders = sorted(named & {p.replace("-", "_") for p in FORBIDDEN_PACKAGES})

    assert offenders == [], f"the spec bundles {offenders}"


# ---------------------------------------------------------------------------
# What the running application imports
# ---------------------------------------------------------------------------

def test_nothing_in_the_application_imports_a_forbidden_module():
    """Catches an import added without a matching requirement line —
    which declared metadata would never show."""
    offenders = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for name in names:
                if name.lower() in FORBIDDEN_IMPORTS:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {name}")

    assert offenders == [], f"forbidden import(s): {offenders}"


# ---------------------------------------------------------------------------
# What the application downloads
# ---------------------------------------------------------------------------

def test_no_download_url_points_at_a_forbidden_component():
    """A model or binary fetched at runtime is as distributed as one in
    the installer."""
    offenders = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        for url in re.findall(r"https?://[^\s\"')]+", text):
            lowered = url.lower()
            if any(bad in lowered for bad in ("espeak", "piper", "/gpl")):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {url}")

    assert offenders == [], f"download URL(s) for forbidden components: {offenders}"


def test_every_download_url_is_pinned_and_checksummed():
    """An unverified download is an unknown component, whatever it was
    supposed to be."""
    from app.voice.kokoro import assets

    for asset in (assets.MODEL_ASSET, *(voice.asset for voice in assets.VOICES)):
        assert assets.MODEL_REVISION in asset.url(), "URLs must pin a revision, not a branch"
        assert len(asset.sha256) == 64
        assert asset.size_bytes > 0


# ---------------------------------------------------------------------------
# The licence record itself
# ---------------------------------------------------------------------------

def test_the_cmu_licence_ships_verbatim():
    licence = (REPO_ROOT / "docs" / "licences" / "CMUDICT-LICENSE.txt").read_text(encoding="utf-8")

    assert "Carnegie Mellon University" in licence
    assert "Redistribution and use" in licence


def test_the_cmu_licence_is_unmodified():
    """Pinned by hash: a licence text that has been edited is not the
    licence."""
    import hashlib

    from app.voice.kokoro.lexicon_source import LICENCE_SHA256

    path = REPO_ROOT / "docs" / "licences" / "CMUDICT-LICENSE.txt"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == LICENCE_SHA256


def test_the_acknowledgement_the_licence_asks_for_is_present():
    from app.voice.kokoro.lexicon_source import ACKNOWLEDGEMENT

    assert "Carnegie Mellon" in ACKNOWLEDGEMENT

    manifest_text = str((REPO_ROOT / "app" / "voice" / "kokoro" / "assets.py").read_text(encoding="utf-8"))
    assert "Carnegie Mellon" in manifest_text


def test_the_cmu_data_is_not_labelled_with_an_spdx_identifier():
    """Its licence is CMU's own text, not literally BSD-2-Clause, and an
    SPDX label would be a factual misstatement."""
    from app.voice.kokoro import assets

    entry = next(
        item for item in assets.LICENCE_MANIFEST if "CMU" in item["component"]
    )
    assert "BSD" not in entry["licence"]
    assert "CMUDICT-LICENSE.txt" in entry["licence"]


def test_the_upstream_data_is_pinned_by_content_hash():
    from app.voice.kokoro.lexicon_source import SOURCE_FILES

    assert SOURCE_FILES
    for source in SOURCE_FILES:
        assert len(source.sha256) == 64
        assert source.size_bytes > 0


# ---------------------------------------------------------------------------
# The installed tree, where one exists
# ---------------------------------------------------------------------------

def _installed_tree():
    """The PyInstaller output, when this runs after a real build."""
    candidate = REPO_ROOT / "packaging" / "dist" / "JARVIS"
    return candidate if candidate.is_dir() else None


def test_the_packaged_tree_check_reports_when_it_cannot_run():
    """A check that silently skips is a check nobody knows is missing."""
    tree = _installed_tree()
    if tree is None:
        pytest.skip(
            "No packaged tree at packaging/dist/JARVIS — this policy is "
            "enforced against the real build in the Windows Installer job."
        )
    assert tree.is_dir()


def test_no_forbidden_binary_ships_in_the_packaged_tree():
    tree = _installed_tree()
    if tree is None:
        pytest.skip("No packaged tree to inspect; enforced in the Windows Installer job.")

    offenders = [
        str(path.relative_to(tree))
        for path in tree.rglob("*")
        if path.is_file() and any(bad in path.name.lower() for bad in FORBIDDEN_BINARIES)
    ]

    assert offenders == [], f"forbidden binaries in the packaged app: {offenders}"


def test_no_forbidden_package_directory_ships_in_the_packaged_tree():
    tree = _installed_tree()
    if tree is None:
        pytest.skip("No packaged tree to inspect; enforced in the Windows Installer job.")

    offenders = [
        str(path.relative_to(tree))
        for path in tree.rglob("*")
        if path.is_dir() and path.name.lower().replace("-", "_") in {
            p.replace("-", "_") for p in FORBIDDEN_PACKAGES
        }
    ]

    assert offenders == [], f"forbidden packages in the packaged app: {offenders}"


def test_the_licence_text_is_protected_from_line_ending_translation():
    """The failure that produced this test.

    The licence hash is pinned, and a Windows checkout rewrote the file's
    LF endings to CRLF — changing its bytes and failing the check on
    Windows CI while passing everywhere else. The check was correct; the
    checkout was the problem, and a "verbatim" licence that git rewrites
    on the way to disk is not verbatim.

    Asserted against .gitattributes rather than against the working copy,
    because the working copy on *this* machine is not where the damage
    happens.
    """
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert "docs/licences/** -text" in attributes, (
        "reproduced licence texts must be exempt from line-ending translation"
    )
    assert "app/voice/kokoro/data/** -text" in attributes, (
        "hash-pinned data must be exempt from line-ending translation"
    )
