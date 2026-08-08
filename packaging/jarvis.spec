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

# Deliberately NOT bundled: .env.example (the packaged app must never
# need it — see app/ui/templates/setup.html and the onboarding flow),
# and faster-whisper (optional, requirements-voice.txt only; the base
# installer doesn't carry real STT inference — see the guided
# model-download flow instead, app/voice/model_installer.py).
datas = [
    (str(repo_root / "app" / "ui" / "templates"), "app/ui/templates"),
    (str(repo_root / "app" / "ui" / "static"), "app/ui/static"),
    (str(repo_root / "docs" / "THIRD_PARTY_NOTICES.md"), "."),
    (str(repo_root / "README.md"), "."),
]

a = Analysis(
    [str(repo_root / "run_jarvis.py")],
    pathex=[str(repo_root)],
    binaries=[],
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
