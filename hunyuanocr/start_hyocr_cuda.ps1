# HunyuanOCR CUDA node start script (idempotent, keepalive-safe)
# engine: PrismML llama.cpp CUDA build (reused from Bonsai deployment, 1660 Ti sm_75)
# models: C:\hyocr\models\ (local copies, no share dependency)
# logging: native --log-file (NEVER use PS "2>&1 | Tee" here: llama logs to stderr,
#          PS 5.1 wraps stderr as ErrorRecord and $ErrorActionPreference=Stop kills the script)
# registered: desktop relay(8765) providers.hunyuanocr -> http://192.168.1.<IP>:8899/v1 (primary)
# VRAM note: holds ~4.7GB of 6GB; run C:\hyocr\stop_hyocr.ps1 before heavy ComfyUI generation
$ErrorActionPreference = 'Stop'
$exe = 'F:\Bonsai\Bonsai-demo\bin\cuda\llama-server.exe'
if (-not (Test-Path $exe)) { Write-Host "engine not found: $exe"; exit 1 }
$mm = 'C:\hyocr\models\HunyuanOCR.Q8_0.gguf'
$proj = 'C:\hyocr\models\HunyuanOCR.mmproj-f16.gguf'
foreach ($f in @($mm, $proj)) { if (-not (Test-Path $f)) { Write-Host "model not found: $f"; exit 1 } }
$conn = Get-NetTCPConnection -LocalPort 8899 -State Listen -ErrorAction SilentlyContinue
if ($conn) { Write-Host 'HunyuanOCR node already running (8899)'; exit 0 }
Write-Host 'Starting HunyuanOCR CUDA node (0.0.0.0:8899)...'
& $exe `
  --model $mm `
  --mmproj $proj `
  --host 0.0.0.0 --port 8899 `
  --alias hunyuanocr `
  --ctx-size 16384 `
  -ngl 99 `
  --repeat-penalty 1.08 `
  --parallel 1 `
  --log-file 'C:\hyocr\node.log'
