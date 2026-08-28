---
name: packaging-verify
description: Verify a PyInstaller build and an Inno Setup installer - contents, hidden imports, data files, checksums, and honest reporting of code-signing and SmartScreen status. Use after producing a Windows build or installer, and before publishing or handing an artifact to a user.
---

# Packaging verification (PyInstaller + Inno Setup)

A build that produced an `.exe` has not been verified. These are the checks that
separate "it built" from "it runs on someone else's machine".

## 1. Build from a known, clean state

```
git rev-parse HEAD                # record the full SHA
git status --porcelain=v1         # must be empty
```

Remove `build/`, `dist/` and `__pycache__` first. Record whether the build was cold.
A build over stale output can silently ship an old module.

## 2. PyInstaller: what usually goes wrong

- **Hidden imports.** Anything imported dynamically (`importlib`, a plugin registry,
  a backend selected at runtime) is invisible to the analysis and is missing at
  runtime. Check the build's warn file (`build/<name>/warn-<name>.txt`) for missing
  modules, and confirm every `hiddenimports` entry in the spec is still needed.
- **Data files.** Icons, templates, model files, `.ui` files and certificates must be
  declared in `datas` and read through the `sys._MEIPASS`-aware resource helper. A
  file that exists next to the script in development will not exist in the bundle.
- **One-file vs one-folder.** One-file extracts to a temp directory on every launch:
  slower start, and any code that writes next to the executable writes into a
  directory that disappears. One-folder starts faster and is easier to debug. Know
  which the project uses and why.
- **Console vs windowed.** A windowed build has no `stdout`. Code that prints, or a
  library that writes to `sys.stderr`, can raise on a `None` stream. Route logging to
  a file.
- **Antivirus and packers.** UPX compression measurably increases false-positive
  detections. If the build uses it, that is a fact to disclose.

Verify the bundle contents rather than trusting the spec:

```
# list what actually shipped
Get-ChildItem -Recurse dist\<name> | Select-Object FullName, Length
```

## 3. Launch the built artifact

Building is not running. On a Windows machine or runner:

- launch the built `.exe` (not the Python entry point) and confirm the main window or
  tray icon appears
- exercise the primary path once
- quit through the app's own quit, and confirm no process remains
- check the log file the app writes, for exceptions that did not surface in the UI

An `.exe` that starts and immediately exits usually means a missing hidden import or
data file — read the log, not the exit code.

## 4. Inno Setup: verify the script, then the installer

In the `.iss` script, check:

- `AppId` is a stable GUID that does not change between versions. If it changes, an
  upgrade installs a second copy instead of upgrading.
- `AppVersion` / `VersionInfoVersion` match the application's own version string.
- `DefaultDirName` uses `{autopf}` (or `{localappdata}` for a per-user install), and
  `PrivilegesRequired` matches: a per-machine install into `Program Files` needs
  admin; a per-user install must not ask for it.
- `[Files]` covers everything the one-folder build produced, not a hand-written list
  that drifts.
- `[UninstallDelete]` removes what the installer created — and **only** that. User
  data must survive an uninstall unless the user asks for a full purge.
- `[Run]` post-install steps do not require a console window or elevate silently.
- The uninstaller is registered and appears in Apps & Features with the right name,
  publisher and version.

Then verify the produced installer by running the lifecycle in
`/install-lifecycle-test`. A script review is not an install test.

## 5. Checksums

Record a digest for every artifact you will refer to later:

```
# PowerShell
Get-FileHash -Algorithm SHA256 dist\<name>-setup.exe
# Linux/macOS
sha256sum dist/<name>-setup.exe
```

```
<artifact>   sha256=<digest>   bytes=<n>   built-from=<full SHA>
```

If a checksum file is published alongside the artifact, verify the artifact against
it and say whether it matched.

## 6. Code signing and SmartScreen — report status, never reassure

Report what you observed, in these exact terms:

| Observation | How to report it |
| --- | --- |
| Authenticode signature present and chain valid | `signed — valid, issuer <name>, expires <date>` |
| Signature present, chain untrusted or expired | `signed — invalid (<reason>)` |
| No signature | `unsigned` |
| Not checked | `signature: not checked` |

```
# PowerShell
Get-AuthenticodeSignature dist\<name>-setup.exe | Format-List Status, StatusMessage, SignerCertificate
```

**SmartScreen disclosure.** An unsigned installer, and a newly signed one without
reputation, will show Windows SmartScreen's "unrecognised app" warning. That is
expected behaviour, not a defect, and users must be told about it in the release
notes. Do not claim SmartScreen behaviour you did not observe: reputation depends on
download volume and history, so it can differ between machines and over time. If you
did not download and run the installer on a real Windows machine, the honest line is
`SmartScreen behaviour: not observed`.

Never write that an artifact is "safe", "trusted" or "will not be flagged".

## 7. Report

```
Commit:       <full SHA>   tree: clean | dirty
Build:        <command> -> exit <code>   (cold | warm)
Warnings:     <missing modules from the warn file, or "none">
Bundle:       <n> files, <size>   — data files present: <list checked>
Launched:     yes on <where> | no — not run
Artifacts:    <path> sha256=<digest> bytes=<n>
Signature:    signed (valid|invalid: <reason>) | unsigned | not checked
SmartScreen:  observed <what> on <machine> | not observed
Unverified:   <what remains — real-machine behaviour, antivirus reaction, upgrade path>
```
