# HunyuanOCR-1.5 llama-server 启动脚本（登录自启/手动启动共用）
# 模型: 腾讯混元OCR-1.5 Q8_0 + mmproj-f16 (C:\Users\?\hyocr\)
# 服务: 127.0.0.1:8899 OpenAI兼容, --alias hunyuanocr, 已注册中转(8765) providers.hunyuanocr
# 注意: 必须 --device none 纯CPU（GTX 660 Kepler 的 Vulkan 会 ErrorDeviceLost 崩溃）
$ErrorActionPreference = 'Stop'
$llamaDir = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe"
$exe = Join-Path $llamaDir 'llama-server.exe'
if (-not (Test-Path $exe)) { Write-Host 'llama-server.exe 未找到, 请先: winget install ggml.llamacpp'; exit 1 }
$conn = Get-NetTCPConnection -LocalPort 8899 -State Listen -ErrorAction SilentlyContinue
if ($conn) { Write-Host 'HunyuanOCR server 已在运行 (8899)'; exit 0 }
Write-Host '启动 HunyuanOCR llama-server ...'
& $exe `
  --model 'C:\Users\?\hyocr\HunyuanOCR.Q8_0.gguf' `
  --mmproj 'C:\Users\?\hyocr\HunyuanOCR.mmproj-f16.gguf' `
  --host 127.0.0.1 --port 8899 `
  --alias hunyuanocr `
  --ctx-size 16384 `
  --device none `
  --repeat-penalty 1.08 `
  --parallel 1
