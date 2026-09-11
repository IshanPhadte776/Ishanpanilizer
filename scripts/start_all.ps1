$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

$nodePath = "C:\Program Files\nodejs"
if (Test-Path $nodePath) {
    $env:Path = "$nodePath;$env:Path"
}

Write-Host "Starting backend ($python -m uvicorn src.api:app) ..."
$backend = Start-Process -FilePath $python `
    -ArgumentList "-m", "uvicorn", "src.api:app", "--host", "127.0.0.1", "--port", "8000" `
    -WorkingDirectory $root -PassThru -NoNewWindow

try {
    Write-Host "Waiting for backend to become healthy ..."
    $healthy = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            Invoke-WebRequest "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 1 | Out-Null
            $healthy = $true
            break
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $healthy) {
        Write-Warning "Backend did not report healthy within 15s; continuing anyway."
    }

    Set-Location (Join-Path $root "ui")
    if (-not (Test-Path "node_modules")) {
        Write-Host "Installing UI dependencies (first run) ..."
        npm install
    }

    Write-Host "Starting UI (npm run dev) ... press Ctrl+C to stop both."
    npm run dev
}
finally {
    if ($backend -and -not $backend.HasExited) {
        Write-Host "Stopping backend ..."
        Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
    }
}
