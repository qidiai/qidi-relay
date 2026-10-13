# keepalive.ps1
# Purpose: run by Windows Task Scheduler every 2 minutes.
# If vibex2api is not answering on its health endpoint, start it again.
# All comments are English on purpose; keep this file ASCII-encoded so
# PowerShell 5.1 does not choke on the code page used by a scheduled task.

$py     = "C:\Program Files\Python312\python.exe"           # absolute path; task PATH may differ
$dir    = Split-Path -Parent $MyInvocation.MyCommand.Path   # vibex2api directory
$script = Join-Path $dir "vibex2api.py"
$log    = Join-Path $dir "service.log"

# Health probe: a 200 from /healthz is proof the service really answers.
$alive = $false
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8787/healthz" -TimeoutSec 5 -UseBasicParsing
    $alive = ($r.StatusCode -eq 200)
} catch {
    $alive = $false
}

if (-not $alive) {
    Start-Process -FilePath $py -ArgumentList "`"$script`"" -WorkingDirectory $dir -WindowStyle Hidden
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  START (was down)" | Add-Content $log
} else {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  ok" | Add-Content $log
}
