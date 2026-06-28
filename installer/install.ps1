#Requires -Version 5.1
<#
.SYNOPSIS
    JARVIS Phase 1 setup script for Windows PowerShell.
.DESCRIPTION
    Checks Python version, creates a virtual environment, installs
    requirements, and prepares the data directory layout.
    Does NOT install Python automatically, does NOT request admin rights,
    does NOT modify system settings.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step([string]$msg) { Write-Host "[JARVIS] $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "[OK]     $msg" -ForegroundColor Green }
function Write-Fail([string]$msg) { Write-Host "[ERROR]  $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  JARVIS Setup — Personal Windows AI Assistant (Phase 1)   " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# --- Check Python ---
Write-Step "Checking Python..."
try {
    $pyVersion = & python --version 2>&1
} catch {
    Write-Fail "Python not found on PATH. Install Python 3.11+ from https://python.org"
    exit 1
}
Write-Ok "$pyVersion"

$minOk = & python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Python 3.11+ required. Please upgrade Python."
    exit 1
}

# --- Virtual environment ---
Write-Step "Setting up virtual environment..."
if (-not (Test-Path ".venv")) {
    & python -m venv .venv
    Write-Ok "Virtual environment created at .venv\"
} else {
    Write-Ok "Virtual environment already exists."
}

# --- Install requirements ---
Write-Step "Installing Python packages..."
& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .\.venv\Scripts\pip.exe install -r requirements.txt --quiet
Write-Ok "Requirements installed."

# --- Data directories ---
Write-Step "Creating data directories..."
@("data\screenshots", "data\logs") | ForEach-Object {
    if (-not (Test-Path $_)) { New-Item -ItemType Directory -Path $_ -Force | Out-Null }
}
Write-Ok "data\screenshots\ and data\logs\ are ready."

# --- .env ---
if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
    Write-Step ".env created from .env.example — add ANTHROPIC_API_KEY if desired."
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Start CLI:  .\.venv\Scripts\python.exe -m app.main"
Write-Host "  Start API:  .\.venv\Scripts\python.exe -m app.api.server"
Write-Host "  API docs:   http://127.0.0.1:5555/docs"
Write-Host ""
