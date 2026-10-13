@echo off
rem Double-click to install simcoach. Runs install.ps1 without changing your
rem PowerShell execution policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
