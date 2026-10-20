@echo off
REM Restart antigravity2api bridge on THIS machine (ASCII only, no BOM).
REM 2026-10-20: Google Interactions API now requires agent+environment; old bridge 502.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'antigravity2api' }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Write-Host ('killed ' + @($p).Count + ' old process(es)') } else { Write-Host 'no running bridge found' }; Start-Sleep -Seconds 2; Start-Process -FilePath 'wscript.exe' -ArgumentList 'run_antigravity.vbs' -WorkingDirectory '%~dp0'; Write-Host 'started via run_antigravity.vbs'"
timeout /t 8 /nobreak >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8791/health' -UseBasicParsing -TimeoutSec 10; Write-Host ('health: ' + $r.Content) } catch { Write-Host ('health FAILED: ' + $_.Exception.Message) }"
pause
