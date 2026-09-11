<#
.SYNOPSIS
    Side-by-side: raw IFC -> GLB on the left, IFC -> CityJSON -> GLB on the right.

.DESCRIPTION
    Exports both pipelines and opens them in two viewports that share one camera, so you
    always see the same angle of each. Left is the unprocessed source (solids, both wall
    faces, interior walls). Right is what the panelizer consumes (single-sided exterior
    facade, interior dropped). No panels in either.

.PARAMETER Ifc
    IFC file to use. Omit to pick from the .ifc files found in the repo.

.PARAMETER SkipConvert
    Reuse the CityJSON already generated for this IFC instead of reconverting.

.PARAMETER ExteriorOnly
    Restrict the raw IFC side to the walls/slabs the converter treats as envelope, which
    makes the two sides far more directly comparable.

.PARAMETER Port
    Port to serve on. Default 8099.

.EXAMPLE
    .\scripts\view_compare.ps1

.EXAMPLE
    .\scripts\view_compare.ps1 -ExteriorOnly -SkipConvert
#>
[CmdletBinding()]
param(
    [string]$Ifc,
    [switch]$SkipConvert,
    [switch]$ExteriorOnly,
    [int]$Port = 8099
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_viewer_common.ps1")

$root = Get-RepoRoot
Set-Location $root
$python = Get-PythonExe -Root $root

$ifcPath = Select-IfcFile -Ifc $Ifc -Root $root
$outDir = Join-Path "input" (Get-ModelSlug -Path $ifcPath)
$cityJson = Join-Path $outDir "model.json"

# ---- left: raw IFC ----
Write-Host ""
Write-Host "== Left: IFC -> GLB (raw) ==" -ForegroundColor Yellow
$rawArgs = @((Join-Path "scripts" "view_model.py"), "--ifc", $ifcPath)
if ($ExteriorOnly) { $rawArgs += "--exterior-only" }
$rawArgs += @("--export-glb", (Join-Path "docs" "model_ifc.glb"))
& $python $rawArgs
if ($LASTEXITCODE -ne 0) { throw "Raw IFC export failed (exit $LASTEXITCODE)" }

# ---- right: IFC -> CityJSON ----
if ($SkipConvert) {
    if (-not (Test-Path $cityJson)) {
        throw "-SkipConvert was passed but $cityJson does not exist yet. Run once without it."
    }
    Write-Host "Reusing existing $cityJson" -ForegroundColor DarkGray
}
else {
    Write-Host ""
    Write-Host "== Stage 1+2: IFC -> LoD3 CityJSON ==" -ForegroundColor Yellow
    & $python (Join-Path "scripts" "ifc_to_cityjson.py") --ifc $ifcPath --out-dir $outDir
    if ($LASTEXITCODE -ne 0) { throw "ifc_to_cityjson.py failed (exit $LASTEXITCODE)" }

    Write-Host ""
    Write-Host "== Stage 3: sanitize (cjio) ==" -ForegroundColor Yellow
    & $python (Join-Path "scripts" "sanitize_cityjson.py") (Join-Path $outDir "model_raw.json") $cityJson
    if ($LASTEXITCODE -ne 0) { throw "sanitize_cityjson.py failed (exit $LASTEXITCODE)" }
}

Write-Host ""
Write-Host "== Right: CityJSON -> GLB ==" -ForegroundColor Yellow
& $python (Join-Path "scripts" "view_model.py") --cityjson $cityJson --export-glb (Join-Path "docs" "model_cityjson.glb")
if ($LASTEXITCODE -ne 0) { throw "CityJSON export failed (exit $LASTEXITCODE)" }

$leftLabel = "1. Raw IFC (no conversion)"
if ($ExteriorOnly) { $leftLabel += " - exterior only" }
$rightLabel = "2. IFC -> CityJSON (LoD3, what the panelizer sees)"

$url = "http://127.0.0.1:$Port/compare.html" +
       "?left=model_ifc.glb&leftLabel=$([uri]::EscapeDataString($leftLabel))" +
       "&right=model_cityjson.glb&rightLabel=$([uri]::EscapeDataString($rightLabel))"

Write-Host ""
Write-Host "== Side by side (shared camera) ==" -ForegroundColor Green
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
