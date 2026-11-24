param([string]$Device)

$ErrorActionPreference = 'Stop'
$server = Join-Path $PSScriptRoot 'llama\llama-server.exe'
$model = Join-Path $PSScriptRoot 'models\Qwen3-8B-Q4_K_M.gguf'

if (-not (Test-Path -LiteralPath $server -PathType Leaf)) {
    throw "llama-server.exe not found: $server"
}
if (-not (Test-Path -LiteralPath $model -PathType Leaf)) {
    throw "Model not found: $model"
}

if (-not $Device) {
    $devices = & $server --list-devices
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $nvidia = $devices | Select-String '^\s*(Vulkan\d+):\s+NVIDIA\b' | Select-Object -First 1
    if (-not $nvidia) {
        throw 'No NVIDIA Vulkan device found. Check the driver or pass -Device from llama-server.exe --list-devices.'
    }
    $Device = $nvidia.Matches[0].Groups[1].Value
}

& $server --model $model --alias qwen3-8b --host 127.0.0.1 --port 8001 --ctx-size 4096 --parallel 1 --device $Device --gpu-layers all --reasoning off --cors-origins localhost --no-cors-credentials
exit $LASTEXITCODE
