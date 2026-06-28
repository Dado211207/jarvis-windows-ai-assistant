@echo off
title JARVIS First-Run Setup
echo ============================================================
echo  JARVIS First-Run Setup
echo ============================================================
echo.

if exist ".env" (
    echo  .env already exists -- no changes made.
    echo.
    goto :done
)

if not exist ".env.example" (
    echo  [!] .env.example not found.
    echo      Please re-download and re-extract the JARVIS ZIP.
    echo.
    pause
    exit /b 1
)

copy ".env.example" ".env" >nul
echo  Created .env from .env.example.
echo.
echo  Next steps:
echo    1. Open .env in Notepad (or any text editor):
echo         notepad .env
echo    2. Find the line:  ANTHROPIC_API_KEY=
echo    3. Paste your Anthropic API key after the = sign.
echo         Example:  ANTHROPIC_API_KEY=sk-ant-...
echo.
echo  IMPORTANT:
echo    Your API key is stored only on YOUR machine.
echo    Do NOT share it in GitHub, Discord, chat, or logs.
echo.

:done
echo  Setup complete. Run START_JARVIS.bat to start JARVIS.
echo.
pause
