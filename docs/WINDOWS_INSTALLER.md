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

## CI: build, installer compile, and smoke test

`.github/workflows/windows-build.yml` runs on every push/PR against
`windows-latest` and:

1. Runs the full test suite and `compileall` (unchanged from before this
   effort).
2. Extracts the version from `app/__init__.py` (`python -c "import app;
   print(app.__version__)"`) — the one place the version is read from.
3. Builds the PyInstaller `--onedir` output.
4. Installs Inno Setup via `choco install innosetup` and compiles
   `installer/JARVIS.iss` into `JARVIS-Setup-<version>.exe`, passing the
   extracted version via `/DMyAppVersion=`.
5. Packages the same PyInstaller output into an optional
   `JARVIS-Portable-<version>.zip` (for users who prefer not to install
   anything — includes `START_JARVIS.bat` / `START_JARVIS_API.bat` /
   `.env.example`, not the primary path).
6. Generates `SHA256SUMS.txt` over both artifacts.
7. Runs an installer smoke test (see below) and uploads all three as build
   artifacts. **Nothing here publishes a GitHub Release or is reachable by
   end users** — see `docs/release-process.md` for the separate, manual,
   tag-triggered release workflow this does not touch.

### Windows Runtime Test Mode

The smoke test never touches a real user's `%LOCALAPPDATA%` and never pops
up a browser tab on the CI runner. Two env vars, both no-ops unless
explicitly set, make this possible:

- `JARVIS_APPDATA_OVERRIDE` (`app/core/paths.py`) — redirects every
  production path (data, logs, cache, backups, config) under a throwaway
  directory instead of the real per-user AppData root.
- `JARVIS_TEST_MODE=1` (`app/core/launcher.py`) — skips only the browser
  auto-open step; logs a warning while active. Nothing about security,
  secret storage, or process execution changes — see the module docstring
  for the exact scope.

The smoke test: silently installs (`/VERYSILENT /SUPPRESSMSGBOXES
/DIR=<temp>`), launches the installed exe with both variables set, polls
`/health` until ready, confirms `/ui/onboarding` responds, confirms the
SQLite database was created under the isolated AppData dir (not the install
dir), confirms no `.env` is required or present, stops the process, then
silently uninstalls and confirms the install directory is gone while the
(isolated) user data directory is preserved — exercising the same
"preserve data by default" path a real user's default uninstall takes,
since `/VERYSILENT` accepts the uninstaller prompt's default answer ("No,
don't delete my data").

## Not yet built here

- A branded `.ico` — the current build uses Inno Setup's default icon, and
  the PyInstaller build does not yet set `--icon` or a Windows version
  resource (`--version-file`) for the exe's file properties. Cosmetic, not
  blocking.
- Update-check UI and the remaining documentation split (`README.md` full
  rewrite, `docs/USER_GUIDE.md`, `docs/SECURITY.md`) are tracked as
  separate follow-up work on the same branch.

## Manual verification

This repository's automation runs in a Linux container with no access to a
real Windows machine and no licensed/installed Inno Setup Compiler — every
piece above (`installer/JARVIS.iss`, the CI workflow's PowerShell smoke
test, `app/core/launcher.py`'s console-hiding and test-mode code) was
written and unit-tested (with the Windows-specific branches mocked out) but
has **not** been executed end-to-end on real Windows from this environment.
`windows-latest` GitHub Actions runners are the actual Windows execution
this relies on for real verification; until a CI run on that runner has
gone green, treat this whole pipeline as reviewed-but-CI-unverified, not as
confirmed working. A real Windows machine is additionally required for the
interactive, visual parts no automation can cover — the installer wizard's
look, the onboarding UI's appearance, and SmartScreen's actual prompt —
none of which this branch's automation attempts to substitute for.
