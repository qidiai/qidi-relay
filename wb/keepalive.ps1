# keepalive for workbuddy2api (port 8768). ASCII only on purpose.
$py     = "C:\Program Files\Python312\python.exe"
$dir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $dir "workbuddy2api.py"
$log    = Join-Path $dir "service.log"
$alive  = $false
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8768/healthz" -TimeoutSec 5 -UseBasicParsing
    $alive = ($r.StatusCode -eq 200)
} catch { $alive = $false }

# Rotate service.log before writing: keep the live log under 1 MB (ASCII only).
if ((Test-Path -LiteralPath $log) -and ((Get-Item -LiteralPath $log).Length -gt 1MB)) {
    Move-Item -LiteralPath $log -Destination (Join-Path $dir "service.log.old") -Force
}

if (-not $alive) {
    Start-Process -FilePath $py -ArgumentList "`"$script`"" -WorkingDirectory $dir -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $dir "adapter.out.log") `
        -RedirectStandardError (Join-Path $dir "adapter.err.log")
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  START (was down)" | Add-Content $log
} else {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  ok" | Add-Content $log
}
