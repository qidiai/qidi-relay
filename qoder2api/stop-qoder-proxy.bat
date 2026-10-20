@echo off
setlocal EnableExtensions
rem ===========================================================
rem  Qoder proxy - stop the detached server (issue #50)
rem
rem  Reads qoder-proxy.pid written by the launcher, verifies the
rem  process command line really contains qoder_proxy.py, then
rem  stops THAT PID only. Never kills other python processes.
rem ===========================================================

set "HERE=%~dp0"
set "PIDFILE=%HERE%qoder-proxy.pid"

if not exist "%PIDFILE%" goto no_pid
set /p PID=<"%PIDFILE%"
if "%PID%"=="" goto no_pid

powershell -NoProfile -ExecutionPolicy Bypass -Command "$id=%PID%; $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $id) -ErrorAction SilentlyContinue; if (-not $p) { Write-Host '[INFO] not running (stale pid file)'; exit 2 }; if ($p.CommandLine -notmatch 'qoder_proxy\.py') { Write-Host ('[WARN] PID ' + $id + ' is not qoder-proxy, refusing to stop: ' + $p.CommandLine); exit 3 }; Stop-Process -Id $id -Force -ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 500; if (Get-Process -Id $id -ErrorAction SilentlyContinue) { Write-Host '[ERROR] still running'; exit 4 }; Write-Host ('[OK] stopped PID ' + $id)"
set "RC=%ERRORLEVEL%"
if exist "%PIDFILE%" del "%PIDFILE%" >nul 2>nul
if "%RC%"=="3" goto end
if "%RC%"=="4" goto end
goto end

:no_pid
echo [INFO] no pid file (%PIDFILE%) - server is not running,
echo        or it was started without --pid-file.
echo        If you started it manually, close that console window.

:end
echo.
if not "%QD_NO_PAUSE%"=="1" pause
