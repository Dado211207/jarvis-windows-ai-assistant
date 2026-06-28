@echo off
title JARVIS Local API (127.0.0.1:5555)
echo ============================================================
echo  JARVIS Local API
echo  Runs locally on:  http://127.0.0.1:5555
echo  Swagger UI:       http://127.0.0.1:5555/docs
echo  The API is NOT exposed to your local network.
echo  Press Ctrl+C to stop.
echo ============================================================
echo.
JARVIS.exe --api
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] JARVIS API exited with error code %ERRORLEVEL%.
    pause
)
