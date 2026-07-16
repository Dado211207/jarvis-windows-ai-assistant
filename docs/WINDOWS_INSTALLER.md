# Windows Installer — Architecture

Status: in progress on `feat/windows-installer-onboarding` (stacked on PR #13,
`feat/persistent-settings-memory`). Not yet released. Sections below cover
what exists today; see the branch's PR description for what's still pending.

## Goal

Replace the ZIP + batch-script distribution with a normal Windows installer:
download `JARVIS-Setup-<version>.exe`, double-click it, no Administrator
prompt, launch from the Start Menu. First-run setup (privacy explanation,
Anthropic API key, voice preference, startup preference) happens inside the
JARVIS UI itself, not in a terminal or a text editor.

## Technology decision

**PyInstaller `--onedir` + Inno Setup**, per-user install.

| Option | Verdict |
|---|---|
| PyInstaller `--onedir` + Inno Setup | **Chosen.** Inno Setup natively supports a per-user install (`PrivilegesRequired=lowest`, `DefaultDirName={localappdata}\...`) with no Administrator prompt, registers a normal Windows uninstaller entry, and its Pascal-scripting hooks (`InitializeUninstall`, `CurUninstallStepChanged`) are enough to implement the opt-in "delete my data" uninstall flow without extra tooling. `--onedir` (vs `--onefile`) avoids the multi-second temp-extraction delay of a onefile build on every launch, at the cost of a directory instead of a single exe — acceptable since Inno Setup packages the whole directory into one `.exe` installer anyway, so the end user only ever sees one file. |
| MSI / WiX | Considered. MSI's native "install for me only" per-user mode exists but is far more constrained (e.g. per-user MSIs can't write to `HKLM`, can't use most bootstrapper features, and per-user Start Menu shortcuts require extra XML ceremony) and WiX's authoring overhead is significantly higher for no corresponding benefit here — JARVIS has no MSI-specific requirement (no Group Policy deployment, no enterprise SCCM story in scope). Not chosen. |
| Squirrel / other auto-update installers | Considered. These are built around silent background self-update, which conflicts with the explicit non-negotiable requirement that update installs always require the user's approval (see "Update experience" below). Not chosen. |

## Layout

- Build output: `dist\JARVIS\` (PyInstaller onedir), then
  `dist\installer\JARVIS-Setup-<version>.exe` (Inno Setup output).
- Install location: `%LOCALAPPDATA%\Programs\JARVIS\` — never
  `C:\Program Files\...` (that requires elevation, which JARVIS's normal
  install never requires).
- User data: `%LOCALAPPDATA%\JARVIS\{data,logs,cache,backups,config}` — see
  `app/core/paths.py`, the single source of truth for every on-disk location.
  This is a **sibling** of the program directory, not nested under it, so
  uninstalling the program never touches user data by construction (the
  uninstaller's `[Files]` removal only ever touches `{app}`, i.e.
  `Programs\JARVIS`).

## The installer script (`installer/JARVIS.iss`)

- `PrivilegesRequired=lowest` — no elevation prompt on normal install.
- `DefaultDirName={localappdata}\Programs\{#MyAppName}`.
- Version comes from `app/__init__.py`'s `__version__` (the single
  authoritative source — see "Versioning" below), passed at compile time:
  `iscc /DMyAppVersion=<version> installer\JARVIS.iss`. The CI build (see
  `.github/workflows/windows-build.yml`) extracts it automatically; nothing
  is hardcoded in the `.iss` file itself.
- Start Menu shortcut always created; desktop shortcut is an opt-in task
  (unchecked by default).
- **Uninstall**: Inno Setup registers the standard Windows "Installed Apps"
  uninstaller automatically. `[Files]` removal only deletes the install
  directory (binaries/shortcuts) — user data is untouched by default.
  `InitializeUninstall()` asks once, with "No" as the default button
  (`MB_DEFBUTTON2`), whether to also delete `%LOCALAPPDATA%\JARVIS` (settings,
  memory, conversation history, logs, the stored API key). Only an explicit
  "Yes" click deletes it; a silent/scripted uninstall or an Enter keypress on
  the default button preserves it.
- **Unsigned.** No code signing is applied, and none is faked. Windows
  SmartScreen will very likely show an "Unrecognized app" warning on first
  run of an unsigned installer from a new publisher — this is expected and
  disclosed, not a bug to work around by disabling SmartScreen. The `.iss`
  script is structured so a real Authenticode signing step (e.g. a
  `SignTool=` directive backed by a certificate provided via CI secrets) can
  be added later without restructuring the installer.

## Not yet built here

- CI wiring to actually run `iscc` and produce `JARVIS-Setup-<version>.exe`
  as a build artifact (tracked separately; the script above is ready to be
  invoked but is not yet exercised by CI on `windows-latest`).
- A branded `.ico` — the current build uses Inno Setup's default icon; the
  PyInstaller build also does not yet set `--icon`. Cosmetic, not blocking.
- Silent-install/uninstall smoke testing, update-check UI, and the remaining
  documentation split (`README.md`, `docs/USER_GUIDE.md`, `docs/SECURITY.md`)
  are tracked as separate follow-up work on the same branch.

## Manual verification

This repository's automation runs in a Linux container with no access to a
real Windows machine or a licensed/installed Inno Setup Compiler, so
`installer/JARVIS.iss` has **not** been compiled or run end-to-end as part of
this change. It is designed to be exercised by `windows-latest` GitHub
Actions runners (which do have `iscc` available via the `chocolatey`/
`innosetup` package or a dedicated setup action) — wiring that up is the
next step, not yet done in this change. Until CI actually compiles and smoke
tests it, treat this script as reviewed-but-unverified.
