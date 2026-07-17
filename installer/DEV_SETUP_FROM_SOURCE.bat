@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo  JARVIS — DEVELOPER environment setup (run from source)
echo  This is NOT the normal install method. If you just want to
echo  use JARVIS, download JARVIS-Setup-^<version^>.exe instead.
echo ============================================================
echo.

:: Check Python version
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on PATH.
    echo Please install Python 3.11+ from https://python.org/downloads/
    echo Make sure to tick "Add Python to PATH" during installation.
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version') do set PY_VER=%%v
echo [OK] Python %PY_VER% found.

:: Check minimum version (3.11)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.11 or higher is required. Current: %PY_VER%
    pause
    exit /b 1
)

:: Create virtual environment
if not exist ".venv" (
    echo [INFO] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created.
) else (
    echo [OK] Virtual environment already exists.
)

:: Activate and install requirements
echo [INFO] Installing requirements...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [ERROR] Failed to install requirements.
    pause
    exit /b 1
)
echo [OK] Requirements installed.

:: Create data directories
if not exist "data\screenshots" mkdir data\screenshots
if not exist "data\logs" mkdir data\logs
echo [OK] Data directories ready.

:: Copy .env if missing
if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env >nul
        echo [INFO] Copied .env.example to .env — add your ANTHROPIC_API_KEY if desired.
    )
)

echo.
echo ============================================================
echo  JARVIS setup complete!
echo ============================================================
echo.
echo  To start the CLI:
echo    .venv\Scripts\activate
echo    python -m app.main
echo.
echo  To start the local API:
echo    .venv\Scripts\activate
echo    python -m app.api.server
echo    Then open: http://127.0.0.1:5555/docs
echo.
pause
