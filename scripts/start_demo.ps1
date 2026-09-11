$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$port = 8098
$url = "http://127.0.0.1:$port"

Set-Location $root

$server = Start-Process -FilePath "python" `
    -ArgumentList "-m", "http.server", "$port", "-d", "docs" `
    -WorkingDirectory $root -PassThru -NoNewWindow

try {
    Write-Host "Serving static demo at $url ... press Ctrl+C to stop."
    Start-Sleep -Seconds 1
    Start-Process $url
    Wait-Process -Id $server.Id
}
finally {
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
}
