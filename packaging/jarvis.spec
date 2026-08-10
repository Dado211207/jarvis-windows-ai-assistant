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
_REQUIRED_PACKAGES = (
    "pydantic_settings", "anthropic", "pyttsx3", "webview", "pythonnet", "clr_loader",
    "faster_whisper", "ctranslate2",
)
# Transitive dependencies of faster-whisper whose exact set varies by
# version. Collected when present, skipped when not — a build must not
# fail because a package the current faster-whisper does not happen to
# depend on is absent, and it must not silently miss one that is.
_OPTIONAL_PACKAGES = ("tokenizers", "onnxruntime", "av", "huggingface_hub")

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
    datas += _pkg_datas
    binaries += _pkg_binaries
    hidden_imports += _pkg_hiddenimports

a = Analysis(
    [str(repo_root / "run_jarvis.py")],
    pathex=[str(repo_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
