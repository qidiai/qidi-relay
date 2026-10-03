# start_relay.ps1 - QiDi Relay (8765) 单点入口: 启动 / 保活 / 防双实例竞态
# 被 ensure_services.ps1、手动 bat、qidicode 等所有拉起点共用的唯一入口。
#
# 用法:
#   start_relay.ps1            健康检查: 已监听 -> already up; 否则清理僵尸并拉起
#   start_relay.ps1 -Force     跳过健康检查, 强制 清理->拉起->等待 (已知要重启的场景)
#
# 退出码: 0=已就绪/拉起成功  1=拉起超时  2=端口被非 relay 进程占用
#
# 注意: 本文件含中文, 必须保存为 UTF-8 WITH BOM (PS5.1 无 BOM 按 GBK 解析会乱码/炸)

param([switch]$Force)

$ErrorActionPreference = "Continue"
$relayDir = $PSScriptRoot           # 脚本所在目录 = relay 目录, 不依赖 cwd
$port     = 8765

function Test-PortUp([int]$port) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $port); $c.Close(); return $true }
    catch { return $false }
}

function Wait-Port([int]$port, [int]$secs) {
    for ($i = 0; $i -lt ($secs * 4); $i++) {
        if (Test-PortUp $port) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function Get-StaleRelayProcesses {
    # 僵尸判定: 命令行含 main.py 的 python/py 进程 = 本 relay 的启动进程。
    # (本目录 "py -3 main.py" 的进程命令行即 "py -3 main.py"; 用全路径 python.exe
    #  起的是 "C:\...\python.exe" main.py。工作目录取不到, 故按规格放宽为全部列出)
    Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -eq "python.exe" -or $_.Name -eq "py.exe") -and $_.CommandLine -match "main\.py"
    }
}

# ---- 1) 端口健康检查 (除 -Force 外必走) ----
if (-not $Force) {
    $listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0) {
        $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
        foreach ($opid in $owners) {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$opid" -ErrorAction SilentlyContinue
            if ($proc -and $proc.CommandLine -match "main\.py") {
                Write-Output "already up, pid=$opid"
                exit 0
            }
        }
        Write-Output "ERROR: port $port 被非 relay 进程占用 (pid=$($owners -join ',')), 拒绝处理"
        exit 2
    }
}

# ---- 2) 僵尸进程清理 (端口没监听但进程还在 = 竞态窗口元凶) ----
$stale = @(Get-StaleRelayProcesses)
if ($stale.Count -gt 0) {
    foreach ($p in $stale) {
        Write-Output "killing stale relay process pid=$($p.ProcessId) name=$($p.Name) cmd=$($p.CommandLine)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Output "no stale relay process found"
}

# ---- 3) WMI 拉起 (Win32_Process.Create 挂到 WmiPrvSE, 脱离调用者存活) ----
Write-Output "spawning: py -3 main.py (cwd=$relayDir)"
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine      = "py -3 main.py"
    CurrentDirectory = $relayDir
}
if ($r.ReturnValue -ne 0) {
    Write-Output "ERROR: Win32_Process.Create failed code=$($r.ReturnValue)"
    exit 1
}

# ---- 4) 等待就绪 (最多 20s, 每 250ms) ----
if (Wait-Port $port 20) {
    Write-Output "started ok, pid=$($r.ProcessId)"
    exit 0
} else {
    Write-Output "START FAILED: relay 20s 内未监听 $port"
    exit 1
}
