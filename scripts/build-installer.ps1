<#
.SYNOPSIS
    Builds the JARVIS Windows installer end to end.

.DESCRIPTION
    Windows-only, deliberately: PyInstaller does not cross-compile a real
    Windows executable from another OS, and this project's rule (see
    CLAUDE.md and packaging/jarvis.spec) is not to attempt it. Run this on
    a real Windows machine, or let .github/workflows/windows-installer.yml
    run it on the windows-latest GitHub Actions runner.

    Ten steps. Each is announced, and a failure in any of them stops the
    build immediately (fail loud, not partially-succeed-and-hope) — a
    broken or incomplete build must never be mistaken for a real one:

      1. Verify this is Windows.
      2. Clean previous build output (packaging\build, packaging\dist).
      3. Verify required tools are available (Python, Inno Setup's ISCC.exe).
      4. Install Python dependencies (requirements.txt + requirements-windows.txt + pinned PyInstaller).
      5. Compile-check (python -m compileall app db).
      6. Run the full test suite (pytest) — never ship a build on a red suite.
      7. Run PyInstaller against packaging\jarvis.spec (onedir build).
      8. Verify the PyInstaller output (JARVIS.exe present; no .env/.env.example/*.db leaked in).
      9. Compile the installer with Inno Setup (ISCC.exe against packaging\jarvis.iss).
      10. Verify the installer output, compute + write its SHA-256, print a summary.

    Version numbers are never a parameter here: packaging/version_info.txt
    and packaging/jarvis.iss both already derive their version from
    app/__init__.py::__version__, and tests/test_packaging_spec.py +
    tests/test_installer_script.py already assert they stay in sync — this
    script just builds whatever those tracked files already declare.

.EXAMPLE
    .\scripts\build-installer.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Write-Step {
    param([int]$Number, [string]$Text)
    Write-Host ""
    Write-Host "=== Step $Number/10: $Text ===" -ForegroundColor Cyan
}

function Assert-LastExitCode {
    # $LASTEXITCODE only reflects the most recently run *native* command —
    # PowerShell's own $ErrorActionPreference does not apply to it, so a
    # failing external tool (pip, pytest, PyInstaller, ISCC.exe) would
    # otherwise be silently ignored and the script would carry on building
    # from broken/partial output. Call this immediately after every native
    # command, never after a pure PowerShell cmdlet (those already throw
    # their own terminating errors under $ErrorActionPreference = "Stop").
    param([string]$Context)
    if ($LASTEXITCODE -ne 0) {
        throw "$Context failed with exit code $LASTEXITCODE"
    }
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# ---------------------------------------------------------------------------
# Step 1: Windows-only guard
# ---------------------------------------------------------------------------
Write-Step 1 "Verify this is Windows"
if ($env:OS -ne "Windows_NT") {
    throw "This script builds a Windows installer and must run on Windows. PyInstaller does not cross-compile a Windows executable from another OS — see packaging/jarvis.spec and CLAUDE.md."
}
Write-Host "OK - running on Windows."

# ---------------------------------------------------------------------------
# Step 2: Clean previous build output
# ---------------------------------------------------------------------------
Write-Step 2 "Clean previous build output"
$BuildDir = Join-Path $RepoRoot "packaging\build"
$DistDir = Join-Path $RepoRoot "packaging\dist"
foreach ($dir in @($BuildDir, $DistDir)) {
    if (Test-Path $dir) {
        Remove-Item -Recurse -Force $dir
        Write-Host "Removed $dir"
    }
}

# ---------------------------------------------------------------------------
# Step 3: Verify required tools
# ---------------------------------------------------------------------------
Write-Step 3 "Verify required tools are available"
$PythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCmd) {
    throw "python was not found on PATH."
}
Write-Host "Python: $($PythonCmd.Source)"

$IsccCandidates = New-Object System.Collections.Generic.List[string]
$IsccOnPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($IsccOnPath) { $IsccCandidates.Add($IsccOnPath.Source) }
$IsccCandidates.Add("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe")
$IsccCandidates.Add("${env:ProgramFiles}\Inno Setup 6\ISCC.exe")
$Iscc = $IsccCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $Iscc) {
    throw "ISCC.exe (the Inno Setup compiler) was not found on PATH or in the usual Program Files locations. Install Inno Setup 6.x, e.g.: choco install innosetup --version=6.7.1"
}
Write-Host "Inno Setup compiler: $Iscc"

# ---------------------------------------------------------------------------
# Step 4: Install Python dependencies
# ---------------------------------------------------------------------------
Write-Step 4 "Install Python dependencies"
python -m pip install -r requirements.txt -r requirements-windows.txt
Assert-LastExitCode "pip install requirements"
python -m pip install pyinstaller==6.21.0
Assert-LastExitCode "pip install pyinstaller"

# ---------------------------------------------------------------------------
# Step 5: Compile check
# ---------------------------------------------------------------------------
Write-Step 5 "Compile check (app + db)"
python -m compileall app db
Assert-LastExitCode "compileall"

# ---------------------------------------------------------------------------
# Step 6: Run the full test suite
# ---------------------------------------------------------------------------
Write-Step 6 "Run the full test suite"
# A temp DB path, same reason as .github/workflows/ci.yml's jobs: never
# leave build-time test artifacts inside the checkout / repo working tree.
$env:JARVIS_DB_PATH = Join-Path $env:TEMP "jarvis_build_installer_test.db"
$env:JARVIS_LOG_LEVEL = "WARNING"
pytest
Assert-LastExitCode "pytest"

# ---------------------------------------------------------------------------
# Step 7: PyInstaller build
# ---------------------------------------------------------------------------
Write-Step 7 "Build the onedir PyInstaller distribution"
# Explicit --distpath/--workpath (rather than relying on PyInstaller's
# default, which is relative to the *current directory at invocation*,
# not the .spec file's own directory) so the output always lands at
# packaging\dist and packaging\build regardless of where this script is
# invoked from — matching packaging\jarvis.iss's "dist\JARVIS\*" Source
# path (resolved relative to jarvis.iss's own directory, packaging\) and
# .gitignore's packaging/dist/ + packaging/build/ entries.
pyinstaller "packaging\jarvis.spec" --distpath "packaging\dist" --workpath "packaging\build" --noconfirm --clean
Assert-LastExitCode "PyInstaller build"

# ---------------------------------------------------------------------------
# Step 8: Verify the PyInstaller output
# ---------------------------------------------------------------------------
Write-Step 8 "Verify the PyInstaller output"
$AppDir = Join-Path $DistDir "JARVIS"
$ExePath = Join-Path $AppDir "JARVIS.exe"
if (-not (Test-Path $ExePath)) {
    throw "Expected build output not found: $ExePath"
}
Write-Host "Found $ExePath"

# Regression guard, not a fix for a known bug: packaging/jarvis.spec's own
# `datas` list is a fixed, enumerated set (templates, static,
# THIRD_PARTY_NOTICES.md, README.md) that never references .env,
# .env.example, a database file, or a log file, so none of these should
# ever be able to appear in real output — this just makes sure a future
# edit to the spec can't silently start bundling them (secrets, local
# test/user data, or logs that could contain either).
$Leaked = Get-ChildItem -Path $AppDir -Recurse -Include ".env", ".env.example", "*.db", "*.log" -ErrorAction SilentlyContinue
if ($Leaked) {
    throw "The PyInstaller output contains files it must never bundle: $($Leaked.FullName -join ', ')"
}
Write-Host "OK - no .env, .env.example, *.db, or *.log files in the build output."

# ---------------------------------------------------------------------------
# Step 9: Inno Setup compile
# ---------------------------------------------------------------------------
Write-Step 9 "Compile the installer with Inno Setup"
& $Iscc "packaging\jarvis.iss"
Assert-LastExitCode "ISCC"

# ---------------------------------------------------------------------------
# Step 10: Verify installer output and compute checksum
# ---------------------------------------------------------------------------
Write-Step 10 "Verify installer output and compute checksum"
$InstallerDir = Join-Path $DistDir "installer"
$InstallerExe = Get-ChildItem -Path $InstallerDir -Filter "JARVIS-Setup-*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $InstallerExe) {
    throw "No installer output found in $InstallerDir"
}
$Hash = Get-FileHash -Path $InstallerExe.FullName -Algorithm SHA256
$ChecksumPath = "$($InstallerExe.FullName).sha256"
# sha256sum-compatible format ("<hash>  <filename>") so it can be verified
# with `sha256sum -c` as well as manually compared.
"$($Hash.Hash.ToLower())  $($InstallerExe.Name)" | Out-File -FilePath $ChecksumPath -Encoding ascii -NoNewline

# Exact byte sizes, reported rather than approximated. Two builds of the
# same commit are not byte-identical (Inno Setup stamps a build time), so
# a size that moves by tens of megabytes between builds is a real change
# worth noticing and a size that moves by a few bytes is not.
$AppExe = Get-ChildItem -Path (Join-Path $DistDir "JARVIS") -Filter "JARVIS.exe" -ErrorAction SilentlyContinue | Select-Object -First 1

Write-Host ""
Write-Host "Build complete." -ForegroundColor Green
Write-Host "Installer:     $($InstallerExe.FullName)"
Write-Host "Installer size: $($InstallerExe.Length) bytes"
if ($AppExe) {
    Write-Host "JARVIS.exe size: $($AppExe.Length) bytes"
}
Write-Host "SHA-256:       $($Hash.Hash)"
Write-Host "Checksum file: $ChecksumPath"
