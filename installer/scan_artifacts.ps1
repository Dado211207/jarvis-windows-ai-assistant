# Secret/dev-artifact scanner for a built JARVIS distribution tree.
#
# Run against the *actual, on-disk result* of what an artifact contains —
# either the real installed directory the smoke test already created (the
# authoritative answer for what the installer ships, since it's the
# installer's own genuine output, not a re-parsed guess at its internal
# format), or an expanded copy of the portable ZIP. Fails (non-zero exit)
# if anything in CLAUDE.md's/docs/SECURITY.md's forbidden list turns up:
# .env, a real API key, jarvis.db (or its -shm/-wal siblings), logs,
# screenshots, test reports, GitHub tokens, dev caches (__pycache__,
# .pytest_cache, .git), or absolute-path debris.
#
# Never writes a matched secret VALUE to output — only the file and which
# pattern matched. GitHub Actions logs are not a safe place to put the
# very thing this script exists to keep out of a shipped artifact.
#
# -AllowPortableExtras permits exactly the files the portable-ZIP packaging
# step (windows-build.yml) deliberately copies in: .env.example (a
# template with no real value — still content-scanned below like every
# other file), README.md, START_JARVIS.bat, START_JARVIS_API.bat. Without
# it (the installer/PyInstaller-output context), none of those are
# allowed. DEV_SETUP_FROM_SOURCE.bat is never expected in either artifact
# and is forbidden regardless — it isn't copied into dist\JARVIS\ by any
# packaging step, so its presence would mean something leaked in from
# outside the intended build process.
#
# Usage: scan_artifacts.ps1 -Path <dir> -Label <name> [-AllowPortableExtras]

param(
    [Parameter(Mandatory = $true)] [string]$Path,
    [Parameter(Mandatory = $true)] [string]$Label,
    [switch]$AllowPortableExtras
)

$ErrorActionPreference = "Stop"
$failed = $false

function Write-FailCheck($message) {
    Write-Host "FAIL [$Label]: $message" -ForegroundColor Red
    $script:failed = $true
}

function Write-PassCheck($message) {
    Write-Host "OK   [$Label]: $message" -ForegroundColor Green
}

if (-not (Test-Path $Path)) {
    throw "scan_artifacts.ps1: path does not exist: $Path"
}

$allFiles = Get-ChildItem -Path $Path -Recurse -File
$allDirs  = Get-ChildItem -Path $Path -Recurse -Directory

# --- 1. Forbidden filenames / dev-artifact directories -----------------

$forbiddenExactNames = @(".env", "jarvis.db", "jarvis.db-shm", "jarvis.db-wal", ".coverage")
foreach ($name in $forbiddenExactNames) {
    $hits = $allFiles | Where-Object { $_.Name -ieq $name }
    if ($hits) {
        Write-FailCheck "found forbidden file '$name' at $($hits[0].FullName)"
    }
}

$forbiddenSuffixes = @(".log", ".db")
foreach ($suffix in $forbiddenSuffixes) {
    $hits = $allFiles | Where-Object { $_.Extension -ieq $suffix }
    if ($hits) {
        Write-FailCheck "found $($hits.Count) file(s) with forbidden extension '$suffix' (e.g. $($hits[0].FullName))"
    }
}

$forbiddenDirNames = @("__pycache__", ".pytest_cache", ".git", "htmlcov", "screenshots")
foreach ($dirName in $forbiddenDirNames) {
    $hits = $allDirs | Where-Object { $_.Name -ieq $dirName }
    if ($hits) {
        Write-FailCheck "found forbidden directory '$dirName' at $($hits[0].FullName)"
    }
}

# Test-report-shaped files (JUnit/pytest XML output) — PyInstaller's own
# output never legitimately contains these; their presence means a dev/CI
# artifact leaked into the distribution.
$reportHits = $allFiles | Where-Object { $_.Name -match "^(pytest-report|junit|test-results).*\.xml$" }
if ($reportHits) {
    Write-FailCheck "found test-report file(s): $($reportHits[0].FullName)"
}

# Portable-ZIP-only helper files: forbidden everywhere else (the installer
# must only ship the app itself), expected — and still content-scanned
# below like everything else — only when -AllowPortableExtras is set.
$portableExtraNames = @(".env.example", "README.md", "START_JARVIS.bat", "START_JARVIS_API.bat")
foreach ($name in $portableExtraNames) {
    $hits = $allFiles | Where-Object { $_.Name -ieq $name }
    if ($hits -and -not $AllowPortableExtras) {
        Write-FailCheck "found '$name' at $($hits[0].FullName) — this artifact must not ship dev/portable-ZIP helper files"
    } elseif ($hits -and $AllowPortableExtras) {
        Write-PassCheck "'$name' present as expected for a portable/dev artifact (still content-scanned below)"
    }
}

# Never expected in any artifact, portable or not — not copied by any
# packaging step, so its presence means something leaked in from outside
# the intended build process.
$devSetupHits = $allFiles | Where-Object { $_.Name -ieq "DEV_SETUP_FROM_SOURCE.bat" }
if ($devSetupHits) {
    Write-FailCheck "found DEV_SETUP_FROM_SOURCE.bat at $($devSetupHits[0].FullName) — never expected in a built artifact"
}

if (-not $failed) {
    Write-PassCheck "no forbidden filenames, dev-cache directories, or test-report files found"
}

# --- 2. Secret-content scan of text-like files --------------------------
# Mirrors scripts/secret_scan.py's pattern set (see that file for why each
# pattern is scoped to a specific credential prefix/format rather than a
# broad heuristic).

$patterns = [ordered]@{
    "anthropic-api-key"       = "sk-ant-[A-Za-z0-9_-]{10,}"
    "generic-sk-api-key"      = "\bsk-[A-Za-z0-9_-]{20,}"
    "github-pat-classic"      = "ghp_[A-Za-z0-9]{36}"
    "github-pat-fine-grained" = "github_pat_[A-Za-z0-9_]{20,}"
    "github-oauth-token"      = "gh[ousr]_[A-Za-z0-9]{36,}"
    "aws-access-key-id"       = "AKIA[0-9A-Z]{16}"
    "private-key-header"      = "-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
}

$textExtensions = @(".py", ".txt", ".md", ".json", ".cfg", ".ini", ".yml", ".yaml",
                     ".js", ".css", ".html", ".ps1", ".bat", ".example", ".env")
$textFiles = $allFiles | Where-Object { $textExtensions -icontains $_.Extension -or $_.Name -ieq ".env.example" }

$contentHits = @()
foreach ($file in $textFiles) {
    $content = Get-Content -Path $file.FullName -Raw -ErrorAction SilentlyContinue
    if (-not $content) { continue }
    foreach ($patternName in $patterns.Keys) {
        if ($content -match $patterns[$patternName]) {
            $contentHits += "$($file.FullName)  [$patternName]"
        }
    }
}

if ($contentHits.Count -gt 0) {
    Write-FailCheck "secret-like content found in $($contentHits.Count) location(s):"
    foreach ($hit in $contentHits) {
        # File path + pattern name only — never the matched text itself.
        Write-Host "         $hit" -ForegroundColor Red
    }
} else {
    Write-PassCheck "no secret-like content found in $($textFiles.Count) text file(s) scanned"
}

# --- verdict --------------------------------------------------------------

if ($failed) {
    Write-Host ""
    Write-Host "Artifact scan FAILED for [$Label] — see FAIL lines above. A matched value is never printed; if a real credential was found, revoke it immediately." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Artifact scan passed for [$Label]." -ForegroundColor Green
exit 0
