---
name: install-lifecycle-test
description: Run the full Windows installer lifecycle - clean install, upgrade, uninstall that preserves user data, full purge, and legacy-data migration - recording what was actually observed on which machine. Use after building an installer, before a release, and when an upgrade or uninstall is reported to lose or leave data.
---

# Installer lifecycle test

Five scenarios. Each needs its own starting state, and each is only reported as
tested if it was actually performed on a Windows machine or runner.

Record for each run: the machine (real PC / VM / CI runner), the Windows version, the
account type (standard or administrator), and the installer's SHA-256.

## Scenario 1 — Clean install

Start from a machine that has never had the app: no install directory, no data
directory, no registry entries, no leftover Start Menu items.

Check:

- the installer runs to completion without an error dialog
- it installs to the intended location and requests elevation only if it needs it
- the app launches from the Start Menu shortcut and from the desktop shortcut if one
  was created
- first-run setup completes and writes its data to the user data directory, not to
  the install directory
- the app appears in Apps & Features with the right name, publisher and version

## Scenario 2 — Upgrade over an existing install

Start from the **previous released version**, with real user data in it: settings
changed from defaults, at least one saved item, and any credential the app stores.

Check:

- the installer detects the existing install and upgrades in place — it does not
  create a second entry in Apps & Features (a changed `AppId` is the usual cause)
- it handles the app being **running** at install time: either it asks to close it,
  or it closes it cleanly. It must not silently replace files under a running process
- after the upgrade: settings survive, saved data survives, stored credentials still
  work, and the version reported by the app is the new one
- the install directory contains no orphaned files from the old version that the new
  version will now load

## Scenario 3 — Uninstall preserving user data

Uninstall through Apps & Features.

Check:

- the uninstaller removes the install directory, shortcuts and its own registry keys
- **user data is left in place** — settings, saved items, logs. Removing them here is
  a data-loss defect, not tidiness
- no running process is left behind
- the Apps & Features entry is gone
- reinstalling afterwards finds the preserved data and uses it

## Scenario 4 — Full purge

The explicit "remove everything" path, whether an uninstaller checkbox, a
documented manual procedure, or a purge command.

Check:

- it is **opt-in** and clearly described. A purge that happens by default is a
  data-loss defect
- it removes the user data directory, cached files, logs, registry entries and any
  stored credential
- after a purge, a fresh install behaves exactly like scenario 1
- list precisely what it removed, so the user can verify

## Scenario 5 — Legacy data migration

Start from an older layout the app must migrate from (an earlier data directory, an
earlier settings format, credentials stored the old way).

Check:

- migration runs once, is idempotent, and is not repeated on every launch
- the original data is preserved or backed up until migration is confirmed — never
  deleted first
- a partial or failed migration leaves the app usable and says what happened
- a *newer* data format encountered by an *older* build fails safely rather than
  corrupting it
- the migration is logged with enough detail to diagnose later

## Verifying what was actually removed or kept

```powershell
# install location and data
Test-Path "$env:ProgramFiles\<AppName>"
Test-Path "$env:LOCALAPPDATA\<AppName>"
Test-Path "$env:APPDATA\<AppName>"

# Apps & Features registration
Get-ItemProperty HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\* ,
                 HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\* |
  Where-Object DisplayName -like '*<AppName>*' |
  Select-Object DisplayName, DisplayVersion, UninstallString

# no process left behind
tasklist /FI "IMAGENAME eq <AppName>.exe"
```

Do not enumerate or print stored credentials. Check only whether the entry exists —
see `/runtime-and-devices`.

## Report

```
Installer:    <path> sha256=<digest>  version=<…>
Machine:      real PC | VM | CI runner — Windows <version> — <standard|admin> account

Scenario 1 clean install    : pass | fail (<what>) | not-run (<why>)
Scenario 2 upgrade          : pass | fail | not-run     from version <…>
Scenario 3 uninstall keeps  : pass | fail | not-run     data checked: <paths>
Scenario 4 full purge       : pass | fail | not-run     removed: <paths>
Scenario 5 migration        : pass | fail | not-run     from layout <…>

Left behind after uninstall: <exact paths and registry keys>
Orphan processes:            none | <names>
Not tested:                  <scenarios and why>
```

A scenario you reasoned about but did not perform is `not-run`. Never report an
installer lifecycle as verified from reading the `.iss` script.
