@echo off
REM Restart the antigravity bridge via the robust PowerShell script.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart_antigravity.ps1"
echo.
type "%~dp0restart_antigravity.result.txt"
pause
