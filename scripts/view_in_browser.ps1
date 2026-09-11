<#
.SYNOPSIS
    Load a model into the browser. Raw IFC by default -- no CityJSON conversion, no panels.

.DESCRIPTION
    Exports the geometry to docs/model.glb and serves docs/ so you can orbit it in the browser.
    With no arguments it finds the .ifc in the repo (prompting if there's more than one).

.PARAMETER Ifc
    IFC file to display. Omit to pick from the .ifc files found in the repo.

.PARAMETER CityJson
    Display a converted CityJSON instead of (or overlaid with) the IFC.

.PARAMETER Classes
    IFC classes to include, comma separated. Default is the envelope set; "all" for everything.

.PARAMETER ExteriorOnly
    Apply the converter's exterior classification, so you see only what the pipeline considers envelope.

.PARAMETER Port
    Port to serve on. Default 8099.

.EXAMPLE
    .\scripts\view_in_browser.ps1
    Pick the IFC, open it in the browser.

.EXAMPLE
    .\scripts\view_in_browser.ps1 -Classes all
#>
[CmdletBinding()]
param(
    [string]$Ifc,
    [string]$CityJson,
    [string]$Classes,
    [switch]$ExteriorOnly,
    [int]$Port = 8099
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

if (-not $Ifc -and -not $CityJson) {
    $candidates = @(
        Get-ChildItem -Path $root -Filter *.ifc -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch '\\\.venv\\' -and $_.FullName -notmatch '\\node_modules\\' } |
            Sort-Object FullName
    )
    if ($candidates.Count -eq 0) {
        throw "No .ifc files found under $root. Pass one with -Ifc <path>."
    }
    elseif ($candidates.Count -eq 1) {
        $Ifc = $candidates[0].FullName
        Write-Host "Using the only IFC found: $($candidates[0].Name)" -ForegroundColor Cyan
    }
    else {
        Write-Host ""
        Write-Host "IFC files found:" -ForegroundColor Cyan
        for ($i = 0; $i -lt $candidates.Count; $i++) {
            $rel = $candidates[$i].FullName.Substring($root.Length + 1)
            Write-Host ("  [{0}] {1}  ({2} MB)" -f ($i + 1), $rel, [math]::Round($candidates[$i].Length / 1MB, 1))
        }
        Write-Host ""
        $choice = Read-Host "Select [1-$($candidates.Count)]"
        $index = 0
        if (-not [int]::TryParse($choice, [ref]$index) -or $index -lt 1 -or $index -gt $candidates.Count) {
            throw "Invalid selection: '$choice'"
        }
        $Ifc = $candidates[$index - 1].FullName
    }
}

$glb = Join-Path "docs" "model.glb"

$argsList = @((Join-Path "scripts" "view_model.py"))
if ($Ifc) { $argsList += @("--ifc", $Ifc) }
if ($CityJson) { $argsList += @("--cityjson", $CityJson) }
if ($Classes) { $argsList += @("--classes", $Classes) }
if ($ExteriorOnly) { $argsList += "--exterior-only" }
$argsList += @("--export-glb", $glb)

Write-Host ""
Write-Host "== Exporting geometry to $glb ==" -ForegroundColor Yellow
& $python $argsList
if ($LASTEXITCODE -ne 0) { throw "Export failed (exit $LASTEXITCODE)" }

$url = "http://127.0.0.1:$Port/model.html"
Write-Host ""
Write-Host "== Serving $url  (Ctrl+C to stop) ==" -ForegroundColor Yellow

$server = Start-Process -FilePath $python `
    -ArgumentList "-m", "http.server", "$Port", "-d", "docs" `
    -WorkingDirectory $root -PassThru -NoNewWindow
try {
    Start-Sleep -Seconds 1
    Start-Process $url
    Wait-Process -Id $server.Id
}
finally {
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
}
