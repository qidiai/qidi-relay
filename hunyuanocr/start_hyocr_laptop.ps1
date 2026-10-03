# HunyuanOCR 笔记本版启动脚本（自动探测 GPU）
# 用法: 先把本文件夹(含 models\)拷到笔记本本地(如 C:\hyocr\), 安装 llama.cpp 后运行本脚本
# 脚本与 models\ 同目录即可自动找到模型
$ErrorActionPreference = 'Stop'
$modelDir = Join-Path $PSScriptRoot 'models'
$mm = Join-Path $modelDir 'HunyuanOCR.Q8_0.gguf'
$proj = Join-Path $modelDir 'HunyuanOCR.mmproj-f16.gguf'
if (-not (Test-Path $mm)) { Write-Host "找不到模型: $mm"; exit 1 }
$llamaDir = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe"
$exe = Join-Path $llamaDir 'llama-server.exe'
if (-not (Test-Path $exe)) { Write-Host 'llama-server.exe 未找到, 请先: winget install ggml.llamacpp'; exit 1 }
$conn = Get-NetTCPConnection -LocalPort 8899 -State Listen -ErrorAction SilentlyContinue
if ($conn) { Write-Host '已在运行 (8899)'; exit 0 }

# 探测显卡: 有 NVIDIA/AMD/Arc 独显或核显就走 Vulkan 加速; 只有老 Intel 核显则纯 CPU
$gpus = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | Where-Object { $_.Name }
$modern = $gpus | Where-Object { $_.Name -match 'NVIDIA|AMD|Radeon|Arc' }
$gpuArgs = @()
if ($modern) {
  Write-Host ('检测到现代显卡: ' + (($modern | Select-Object -First 1).Name) + ' -> Vulkan 加速模式')
  $gpuArgs = @('-ngl','99')
} else {
  Write-Host ('仅检测到: ' + (($gpus | Select-Object -First 1).Name) + ' -> 纯 CPU 模式')
  $gpuArgs = @('--device','none')
}
Write-Host '启动 HunyuanOCR llama-server (0.0.0.0:8899, alias hunyuanocr)...'
& $exe `
  --model $mm `
  --mmproj $proj `
  --host 0.0.0.0 --port 8899 `
  --alias hunyuanocr `
  --ctx-size 16384 `
  @gpuArgs `
  --repeat-penalty 1.08 `
  --parallel 1
# 提示: 若 Vulkan 模式出现 "ErrorDeviceLost" 崩溃, 把 $gpuArgs 那行改为 @('--device','none') 再跑
