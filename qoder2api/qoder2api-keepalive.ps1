# qoder2api keepalive watchdog
# Scheduled daily 08:50 / 20:50 (before the 09:00/21:00 checkin windows):
# if the bridge (127.0.0.1:8790) is not listening, restart it via its task.
$ErrorActionPreference = "SilentlyContinue"
$log = "G:\程序开发\qoder2api-audit\qoder2api-keepalive.log"
$up = $false
try {
    $c = New-Object Net.Sockets.TcpClient
    $iar = $c.BeginConnect("127.0.0.1", 8790, $null, $null)
    $up = $iar.AsyncWaitHandle.WaitOne(3000, $false) -and $c.Connected
    $c.Close()
} catch { }
if ($up) {
    Add-Content $log ((Get-Date -Format "MM-dd HH:mm:ss") + "  ok: bridge listening")
} else {
    Add-Content $log ((Get-Date -Format "MM-dd HH:mm:ss") + "  bridge DOWN -> restarting task qoder2api")
    schtasks /run /tn "qoder2api" | Out-Null
    Start-Sleep -Seconds 12
    try {
        $c2 = New-Object Net.Sockets.TcpClient
        $iar2 = $c2.BeginConnect("127.0.0.1", 8790, $null, $null)
        $ok2 = $iar2.AsyncWaitHandle.WaitOne(8000, $false) -and $c2.Connected
        $c2.Close()
        Add-Content $log ((Get-Date -Format "MM-dd HH:mm:ss") + ("  restart -> " + $(if ($ok2) { "UP" } else { "STILL DOWN" })))
    } catch {
        Add-Content $log ((Get-Date -Format "MM-dd HH:mm:ss") + "  restart check failed")
    }
}
