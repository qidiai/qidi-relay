@echo off
setlocal EnableExtensions
rem ===========================================================
rem  Qoder proxy - launcher (CN + Intl)  ** detached mode **
rem  Usage: double-click, or  start-qoder-proxy.bat [port]
rem
rem  issue #50: the server now runs DETACHED. Closing this
rem  window no longer stops it; use stop-qoder-proxy.bat.
rem
rem  ASCII-only on purpose: .bat files are read using the
rem  console code page, so non-ASCII text breaks the parser.
rem ===========================================================

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8790"
set "HERE=%~dp0"
set "SCRIPT=%HERE%qoder_proxy.py"
set "PIDFILE=%HERE%qoder-proxy.pid"
set "LOGFILE=%HERE%qoder-proxy.log"
set "ERRFILE=%HERE%qoder-proxy.err.log"

if not exist "%SCRIPT%" goto no_script

set "PYEXE="

if not exist "%HERE%python\python.exe" goto try_path
"%HERE%python\python.exe" --version >nul 2>nul
if errorlevel 1 goto try_path
set "PYEXE=%HERE%python\python.exe"
goto check_running

:try_path
python --version >nul 2>nul
if errorlevel 1 goto try_py
set "PYEXE=python"
goto check_running

:try_py
py --version >nul 2>nul
if errorlevel 1 goto no_python
set "PYEXE=py"
goto check_running

:no_python
echo [ERROR] No usable Python found.
echo   Use the packaged zip (contains python\), or install Python 3.9+.
pause
exit /b 1

:no_script
echo [ERROR] qoder_proxy.py not found next to this script.
pause
exit /b 1

:check_running
if not exist "%PIDFILE%" goto launch
set /p OLDPID=<"%PIDFILE%"
if "%OLDPID%"=="" goto launch
tasklist /FI "PID eq %OLDPID%" 2>nul | find "%OLDPID%" >nul
if errorlevel 1 goto launch
echo [INFO] qoder-proxy already running (PID %OLDPID%).
echo        Dashboard: http://127.0.0.1:%PORT%/
echo        Stop it with: stop-qoder-proxy.bat
goto end

:launch
echo Starting qoder-proxy on port %PORT% (detached) ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Start-Process -FilePath '%PYEXE%' -ArgumentList '%SCRIPT%','--port','%PORT%','--pid-file','%PIDFILE%' -WindowStyle Hidden -RedirectStandardOutput '%LOGFILE%' -RedirectStandardError '%ERRFILE%' -PassThru; Start-Sleep -Milliseconds 1500; if ($p.HasExited) { Write-Host '[WARN] exited early - see qoder-proxy.log' } else { Write-Host ('[OK] started, PID ' + $p.Id) }"
echo.
echo   API      : http://127.0.0.1:%PORT%/v1
echo   Dashboard: http://127.0.0.1:%PORT%/
echo   Log      : %LOGFILE%
echo.
echo   This window can be closed now - the server keeps running.
echo   Stop it with: stop-qoder-proxy.bat

:end
echo.
if not "%QD_NO_PAUSE%"=="1" pause
