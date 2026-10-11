param(
    [string]$PythonCommand = 'python',
    [switch]$SkipBmad
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $ProjectRoot
try {
    & $PythonCommand -c "import sys; assert sys.version_info >= (3, 10), 'Bootstrap requires Python >= 3.10'"
    if ($LASTEXITCODE -ne 0) { throw 'A usable Python interpreter is required.' }
    $UvPath = Join-Path $ProjectRoot '.tools/python/bin/uv.exe'
    if (-not (Test-Path -LiteralPath $UvPath)) {
        & $PythonCommand -m pip install --disable-pip-version-check --no-warn-script-location --target '.tools/python' 'uv==0.13.0'
        if ($LASTEXITCODE -ne 0) { throw 'uv installation failed.' }
    }
    . (Join-Path $PSScriptRoot 'env.ps1')
    & $UvPath sync --locked --python 3.12
    if ($LASTEXITCODE -ne 0) { throw 'Python environment setup failed.' }
    & $UvPath run --locked python scripts/check_env.py
    if ($LASTEXITCODE -ne 0) { throw 'Environment check failed.' }
    if (-not $SkipBmad) {
        & npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw 'BMAD dependency installation failed.' }
        & npm.cmd run bmad:install
        if ($LASTEXITCODE -ne 0) { throw 'BMAD workspace installation failed.' }
    }
    Write-Host 'LabQueue environment is ready. Run: .\scripts\dev.ps1'
} finally {
    Pop-Location
}
