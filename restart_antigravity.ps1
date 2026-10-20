# Restart antigravity2api bridge (ASCII only, no BOM).
# 2026-10-21: inline -Command quoting in the old .bat silently failed to kill the
# old process, so the new code never took effect. This script does kill/verify/
# start/report with explicit steps, and writes restart_antigravity.result.txt so
# another machine (via SMB) can confirm what actually happened.
$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$result = Join-Path $here 'restart_antigravity.result.txt'
function W([string]$m) { $line = $m; Write-Host $line; Add-Content -Path $result -Value $line -Encoding UTF8 }

W ("=== " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + " restart ===")

# 1) kill every python process whose command line mentions antigravity2api
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
           Where-Object { $_.CommandLine -like '*antigravity2api*' })
if ($procs.Count -gt 0) {
    foreach ($p in $procs) {
        W ("kill pid=" + $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else {
    W 'no bridge process found'
}

# 2) wait for port 8791 to be released
$freed = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    $ok = Test-NetConnection -ComputerName 127.0.0.1 -Port 8791 -InformationLevel Quiet -WarningAction SilentlyContinue
    if (-not $ok) { $freed = $true; break }
}
W ("port 8791 released: " + $freed)
if (-not $freed) {
    W 'ABORT: port 8791 still in use; kill the old process manually then rerun'
    exit 1
}

# 3) start detached via the existing VBS launcher
Start-Process -FilePath 'wscript.exe' -ArgumentList (Join-Path $here 'run_antigravity.vbs') -WorkingDirectory $here
Start-Sleep -Seconds 6

# 4) health check (new build returns a "build" field)
try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8791/health' -UseBasicParsing -TimeoutSec 10
    W ("health: " + $r.Content)
} catch {
    W ("health FAILED: " + $_.Exception.Message)
}

# 5) report the new process
$now = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
         Where-Object { $_.CommandLine -like '*antigravity2api*' })
if ($now.Count -eq 0) { W 'WARN: no bridge process after start' }
foreach ($p in $now) { W ("running pid=" + $p.ProcessId + " started=" + $p.CreationDate) }
W '=== done ==='
