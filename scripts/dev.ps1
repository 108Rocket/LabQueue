param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$UvPath = Join-Path $ProjectRoot '.tools/python/bin/uv.exe'
if (-not (Test-Path -LiteralPath $UvPath)) { throw 'Run scripts/bootstrap.ps1 first.' }
. (Join-Path $PSScriptRoot 'env.ps1')
Push-Location $ProjectRoot
try {
    & $UvPath run --locked uvicorn labqueue.app:app --reload --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'LabQueue development server exited with an error.' }
} finally {
    Pop-Location
}
