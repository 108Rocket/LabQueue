# Dot-source this file to make project-local uv available in the current terminal.
$LabQueueUvDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) '.tools/python/bin'
if (-not (Test-Path -LiteralPath (Join-Path $LabQueueUvDirectory 'uv.exe'))) {
    throw 'Run scripts/bootstrap.ps1 before loading this environment.'
}
if (($env:PATH -split ';') -notcontains $LabQueueUvDirectory) {
    $env:PATH = $LabQueueUvDirectory + ';' + $env:PATH
}
