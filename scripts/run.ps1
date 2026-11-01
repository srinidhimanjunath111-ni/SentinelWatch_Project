# Create the virtual environment if needed, install requirements, and start SentinelWatch.
# PyTorch is installed so the trained models run; pass -NoModels to skip it, in which case
# the pipeline uses its labelled spectral baseline.
param([switch]$NoModels)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$port = if ($env:PORT) { $env:PORT } else { "8000" }
$bindHost = if ($env:HOST) { $env:HOST } else { "127.0.0.1" }
$python = ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creating .venv"
    py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) { python -m venv .venv }
}

$stamp = ".venv\.requirements-installed"
$stale = -not (Test-Path $stamp)
if (-not $stale) {
    $stamped = (Get-Item $stamp).LastWriteTime
    $stale = ((Get-Item requirements-operational.txt).LastWriteTime -gt $stamped) -or
             ((Get-Item requirements-core.txt).LastWriteTime -gt $stamped)
}
if ($stale) {
    Write-Host "Installing requirements"
    & $python -m pip install --quiet --upgrade pip
    & $python -m pip install --quiet -r requirements-operational.txt
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    New-Item -ItemType File -Force -Path $stamp | Out-Null
}

if (-not $NoModels) {
    & $python -c "import torch" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing PyTorch"
        & $python -m pip install --quiet torch
        if ($LASTEXITCODE -ne 0) {
            Write-Host "PyTorch could not be installed; continuing with the spectral baseline."
        }
    }
}

Write-Host "Starting SentinelWatch on http://127.0.0.1:$port"
& $python -m uvicorn app.main:app --host $bindHost --port $port --timeout-graceful-shutdown 5
exit $LASTEXITCODE
