@echo off
title JARVIS CLI
echo ============================================================
echo  JARVIS -- Personal Windows AI Assistant
echo  Type 'help' to list commands, 'exit' to quit.
echo ============================================================
echo.
JARVIS.exe
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] JARVIS exited with error code %ERRORLEVEL%.
    pause
)
