# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the installed JARVIS.exe: a single, windowed
(no console — see run_jarvis.py's own docstring for why), onedir build.

Built by scripts/build-installer.ps1 via `pyinstaller packaging/jarvis.spec`
— not run directly during normal development, and not what
.github/workflows/windows-build.yml's separate, pre-existing,
console-mode build job uses (that job is untouched by this pass; see
the packaging report for why the two coexist).

Tracked in git (see the `!packaging/*.spec` exception in .gitignore):
pinning exactly what gets bundled — hidden imports, data files, the
version resource — is itself part of this project's source, not
throwaway build output. Only PyInstaller's own *output* (build/, dist/)
stays gitignored.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None
repo_root = Path(SPECPATH).resolve().parent  # packaging/jarvis.spec -> repo root

# Hidden imports PyInstaller's static analysis cannot see on its own:
#   - pyttsx3's SAPI5 driver and comtypes: pyttsx3 selects its driver
#     dynamically by platform (carried over from the pre-existing
#     console-mode build job, which already found these necessary).
#   - keyring.backends.Windows: keyring discovers backends via
#     importlib.metadata entry points, not static imports — without
#     this, a frozen build has no working credential-store backend at
#     all, and app/core/credentials.py's own "fails safe, never raises"
#     design would silently hide that (get_stored_api_key() would just
#     always report "nothing stored").
hidden_imports = [
    "pyttsx3.drivers.sapi5",
    "comtypes",
    "comtypes.client",
    "keyring.backends.Windows",
]

# webview/pythonnet (native desktop window, release-candidate packaging
# pass — see app/launcher/webview_window.py and
# docs/THIRD_PARTY_NOTICES.md): included in the collect_all() loop below
# rather than just hiddenimports, on the same "known empirically, not by
# guesswork, to need full submodule+data collection" basis already
# established for pydantic_settings/anthropic/pyttsx3 — a frozen build
# that silently fails to find its native GUI backend would fail exactly
# the same "process stays alive, health check never answers" way that
# happened before, hard to tell apart from any other startup issue
# without a real windows-latest CI run to confirm it either way.

# Deliberately NOT bundled: .env.example (the packaged app must never
# need it — see app/ui/templates/setup.html and the onboarding flow).
#
# faster-whisper IS bundled now. It was not, and the consequence was that
# the installed app could never transcribe anything: the Voice page
# offered push-to-talk, the setup screen reported "Speech runtime — Not
# ready", and no action available to a user could change either. The
# engine is code and ships with the app; the model is data and is still
# downloaded on request with its licence, size and checksum shown first
# (app/voice/model_installer.py).
datas = [
    (str(repo_root / "app" / "ui" / "templates"), "app/ui/templates"),
    (str(repo_root / "app" / "ui" / "static"), "app/ui/static"),
    (str(repo_root / "docs" / "THIRD_PARTY_NOTICES.md"), "."),
    (str(repo_root / "README.md"), "."),
    # Reproduced licence texts travel with the product, not with a link:
    # the obligation is to the person holding the binary.
    (str(repo_root / "docs" / "licences"), "docs/licences"),
    # The pronunciation lexicon. PyInstaller does not collect package
    # data for the application's own modules — only for third-party
    # packages via collect_all — so without this line the installed app
    # has a voice that cannot pronounce anything and falls back to
    # spelling every word.
    (str(repo_root / "app" / "voice" / "kokoro" / "data"), "app/voice/kokoro/data"),
]
binaries = []

# collect_all(), not just hiddenimports: these three packages are
# already known — empirically, not by guesswork — to need PyInstaller's
# full submodule+data collection, not just its default static-import
# analysis. The pre-existing .github/workflows/windows-build.yml build
# job (separate from this one, untouched by this pass) already passes
# `--collect-all pydantic_settings --collect-all anthropic --collect-all
# pyttsx3` on the PyInstaller command line for exactly this reason; this
# spec had only carried the narrower pyttsx3 driver hidden-import above,
# which was not enough on its own — a real frozen JARVIS.exe launched on
# windows-latest CI stayed running but never answered /health, matching
# an import failing silently inside the background uvicorn thread
# (app/launcher/server_runner.py) rather than crashing the process
# outright. collect_all is additive and can only include more than the
# default analysis would, so applying it here is a safe, proven step
# regardless of exactly which submodule was missing.
#
# faster_whisper/ctranslate2/tokenizers/onnxruntime/av are here for the
# same empirical reason: ctranslate2 is a compiled extension with its own
# bundled DLLs, and tokenizers/onnxruntime carry data files PyInstaller's
# static analysis does not follow.
#
# onnxruntime is required, not optional: it is what runs the neural
# voice. A build without it produces an application whose speech falls
# straight through to the robotic tier with no way for a user to fix it,
# which is the exact failure this release exists to correct.
_REQUIRED_PACKAGES = (
    "pydantic_settings", "anthropic", "pyttsx3", "webview", "pythonnet", "clr_loader",
    "faster_whisper", "ctranslate2", "onnxruntime", "numpy",
    # faster-whisper's own declared hard dependencies. These were in the
    # optional list, and that is why the shipped release candidate had no
    # speech input at all: `av` is imported by faster_whisper.audio at
    # package import time, so when its collection was skipped the
    # installed app's very first `import faster_whisper` raised
    # ImportError — reported to the user as "The local speech engine
    # isn't available in this installation", with reinstalling the same
    # artifact as the suggested fix.
    #
    # Read from faster-whisper 1.2.0's own metadata, not guessed:
    #   ctranslate2, huggingface-hub, tokenizers, onnxruntime, av, tqdm.
    # Every one is required at import time, so every one belongs here
    # where a failure to collect stops the build instead of printing
    # "skipping" and producing a broken product.
    "av", "tokenizers", "huggingface_hub", "tqdm",
    # And one level deeper again, proven the same way. With the six above
    # bundled, the frozen build still could not import faster_whisper:
    #
    #   FAILED  Speech recognition (faster-whisper): No module named 'requests'
    #
    # huggingface_hub reaches `requests` through a path PyInstaller's
    # static analysis does not follow, so nothing in the module graph
    # pulled it in. It is a hard dependency of a hard dependency, which
    # makes it exactly as load-bearing as the direct ones — the installed
    # app is equally unable to transcribe without it.
    #
    # Its own dependencies (certifi, charset_normalizer, idna, urllib3)
    # are reached by ordinary analysis once `requests` itself is in the
    # graph, and are deliberately not listed: a name here that does not
    # need to be would be guesswork, and the frozen self-test names
    # anything still missing rather than leaving it to be discovered by
    # a user.
    "requests",
)
# Genuinely optional: absence changes no capability the product claims.
# Nothing whose absence breaks an advertised feature may live here — see
# the note above for what that cost last time.
#
# winsdk is optional on purpose: it is the WinRT projection behind the
# second speech tier, and a build that cannot collect it should produce
# an application whose Windows-natural-voice tier reports itself
# unavailable — the same thing it does on a machine without it — rather
# than no application at all.
_OPTIONAL_PACKAGES = ("winsdk",)

# Nothing named after a copyleft speech engine may reach the installed
# tree, and collect_all() will put it there by default if left alone:
# it passes include_py_files=True, which copies every .py in a collected
# package into _internal/ as a loose data file on top of the module in
# the PYZ archive. That is how pyttsx3's espeak ctypes bindings shipped
# in the release candidate — caught by tests/test_licence_policy.py
# running against the real installed tree in the Windows Installer job,
# and by nothing before it.
#
# Those bindings are pyttsx3's own source, not GPL text; what they are
# is a loader for libespeak, which is GPL. This product's rule is that no
# GPL component is bundled, imported, invoked or downloaded, and shipping
# the loader for one in a Windows-only application that can never select
# it is all licence surface and no function: pyttsx3 chooses its driver
# dynamically by platform (importlib.import_module) and chooses sapi5
# here, which is hidden-imported above and unaffected.
#
# Enforced twice deliberately. _forbidden_file() drops them from the
# collected file lists, and _EXCLUDED_MODULES keeps them out of the
# module graph so they cannot arrive through the PYZ archive instead.
_FORBIDDEN_FILE_MARKERS = ("espeak", "piper")

_EXCLUDED_MODULES = [
    "pyttsx3.drivers.espeak",
    "pyttsx3.drivers._espeak",
]


def _forbidden_file(entry):
    """True for a (source, destination) pair naming a forbidden engine.

    Matched on the file name, the same way the packaged-tree test reads
    the installed app, so the spec and the check cannot disagree about
    what counts.
    """
    source = str(entry[0] if isinstance(entry, (tuple, list)) else entry)
    name = Path(source).name.lower()
    return any(marker in name for marker in _FORBIDDEN_FILE_MARKERS)


def _forbidden_module(name):
    if name in _EXCLUDED_MODULES:
        return True
    leaf = name.rsplit(".", 1)[-1].lower().lstrip("_")
    return any(marker == leaf for marker in _FORBIDDEN_FILE_MARKERS)


for _pkg in _REQUIRED_PACKAGES + _OPTIONAL_PACKAGES:
    try:
        _pkg_datas, _pkg_binaries, _pkg_hiddenimports = collect_all(_pkg)
    except Exception as _exc:  # noqa: BLE001
        if _pkg in _REQUIRED_PACKAGES:
            raise SystemExit(
                f"packaging/jarvis.spec: required package {_pkg!r} could not be collected "
                f"({_exc}). The installer must not be built without it — a JARVIS.exe "
                "missing one of these is broken in a way that only shows up at runtime."
            )
        print(f"packaging/jarvis.spec: optional package {_pkg!r} not present; skipping.")
        continue
    _dropped = [entry for entry in _pkg_datas + _pkg_binaries if _forbidden_file(entry)]
    for _entry in _dropped:
        print(f"packaging/jarvis.spec: dropping {Path(str(_entry[0])).name} from {_pkg} (licence policy)")
    datas += [entry for entry in _pkg_datas if not _forbidden_file(entry)]
    binaries += [entry for entry in _pkg_binaries if not _forbidden_file(entry)]
    hidden_imports += [name for name in _pkg_hiddenimports if not _forbidden_module(name)]

a = Analysis(
    [str(repo_root / "run_jarvis.py")],
    pathex=[str(repo_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_EXCLUDED_MODULES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JARVIS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(repo_root / "app" / "ui" / "static" / "icon.ico"),
    version=str(repo_root / "packaging" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JARVIS",
)
