---
name: windows-ci-portability
description: Make a Python desktop project testable on a Windows CI runner - repeated start and quit cycles, orphan-process detection, path and encoding portability, and an explicit list of what a runner cannot prove. Use when adding or debugging Windows CI, or when deciding whether a green Windows job justifies a release.
---

# Windows CI portability

A Windows runner is a headless VM with no sound device, no camera, no GPU
acceleration, no interactive desktop session in the usual sense, and no user. It can
prove a lot, and it cannot prove the things users notice most. Both halves belong in
the report.

## 1. What a Windows runner can prove

- the project installs its dependencies on Windows
- imports resolve, and the app's modules load under the Windows path rules
- unit and integration tests pass on Windows
- the PyInstaller build completes and produces the expected files
- the Inno Setup compile succeeds
- the built `.exe` starts and exits cleanly, repeatedly
- no orphan process is left behind
- file paths, encodings and line endings behave

## 2. What it cannot prove

State these explicitly every time, so a green badge is never mistaken for coverage:

- audio output, microphone input, speech recognition quality, voice quality
- camera, GPU-accelerated rendering, display scaling and multi-monitor behaviour
- antivirus and SmartScreen reaction on a real machine with real reputation data
- the real installer UI, elevation prompts, and Windows privacy permission dialogs
- how the application feels: responsiveness, tray discoverability, visual correctness
- upgrade from a version a real user actually had, with their real data

## 3. Repeated start / quit cycles

The cheapest high-value Windows CI check. Start the built app, wait for readiness,
quit it through its own path, and confirm it exited — then do it several times.

```powershell
$exe  = "dist\<name>\<name>.exe"
$fail = 0
foreach ($i in 1..5) {
  $p = Start-Process -FilePath $exe -PassThru
  Start-Sleep -Seconds 5                     # replace with a real readiness signal
  if ($p.HasExited) { Write-Host "run ${i}: exited early, code $($p.ExitCode)"; $fail++ ; continue }
  $p.CloseMainWindow() | Out-Null
  if (-not $p.WaitForExit(10000)) { Write-Host "run ${i}: did not exit in 10s"; $p.Kill(); $fail++ }
}
if ($fail -gt 0) { exit 1 }
```

Prefer a real readiness signal over `Start-Sleep`: a log line, a created file, or the
localhost port accepting a connection. A fixed sleep makes the check flaky and hides
slow starts. If you use a sleep, say so, and say what would replace it.

## 4. Orphan-process detection

After each quit, no process from the app may remain:

```powershell
$left = Get-Process -Name "<name>" -ErrorAction SilentlyContinue
if ($left) { $left | Select-Object Id, ProcessName, StartTime; exit 1 }
```

Also check children the app spawns by name. An orphan on a runner means an orphan on
a user's machine, where it holds a lock, a port, or a device.

## 5. Path and encoding portability

Fail the build for the portability defects that Linux development hides:

- **Case sensitivity in reverse**: an import or file reference with the wrong case
  works on Windows and breaks on Linux. Keep a Linux job too if the code is shared.
- **Path separators**: no hard-coded `/` or `\`. Use `pathlib`.
- **Path length**: test with a deep working directory; the classic 260-character
  limit still bites without long-path support enabled.
- **Spaces and non-ASCII**: run at least one job from a directory whose name contains
  a space and a non-ASCII character.
- **Encoding**: set `PYTHONUTF8=1` or pass `encoding=` explicitly. The default
  console encoding on Windows is not UTF-8, and a stray non-ASCII character in output
  will raise `UnicodeEncodeError` in a place that has nothing to do with the change.
- **Line endings**: commit a `.gitattributes`. A test that compares file bytes will
  fail on Windows if git rewrote the newlines.
- **Reserved names**: `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9` cannot
  be file names. Generated names derived from user input must be sanitised.

## 6. Runner hygiene

- pin the runner image (`windows-2022`) rather than `windows-latest`, so an image
  change is a deliberate commit and not a surprise failure
- cache dependencies by lockfile hash
- upload the build log, the PyInstaller warn file, and the app's own log as artifacts
  on failure — a Windows failure you cannot see is a failure you cannot fix
- give the job a timeout so a hung GUI process cannot consume the whole budget
- do not put signing certificates or store credentials in a workflow that runs on
  pull requests from forks

## 7. Report

```
Runner:        <image> — Windows <version>
Jobs:          <name> -> exit <code>   (<n> passed, <n> failed, <n> skipped)
Start/quit:    <n> of <n> cycles clean   (readiness: <signal | fixed sleep of Ns>)
Orphans:       none | <process names>
Portability:   <checks run: long path, spaces, non-ASCII, encoding, line endings>
Artifacts:     <what was uploaded>
Proves:        <the specific claims>
Does not prove: audio, microphone, speech quality, camera, GPU, antivirus,
                SmartScreen reputation, installer UI, permission dialogs, real
                upgrade from a user's data, and how the app feels
```
