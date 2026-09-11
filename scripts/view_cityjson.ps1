<#
.SYNOPSIS
    IFC -> CityJSON -> GLB -> browser. Runs the conversion. NO panels.

.DESCRIPTION
    Runs the pipeline's first three stages -- ifc_to_cityjson.py, then cjio sanitize --
    and displays the resulting LoD3 CityJSON from docs/model_cityjson.glb.

    This is what the panelizer actually consumes, so it looks deliberately thinner than
    the raw IFC: single-sided exterior facade only, interior walls dropped, each wall one
    planar polygon rather than a solid. Coloured by CityJSON semantic type.

    For the unprocessed source geometry, use view_ifc.ps1.

.PARAMETER Ifc
    IFC file to convert and display. Omit to pick from the .ifc files found in the repo.

.PARAMETER CityJson
    Skip conversion and display an existing CityJSON file directly.

.PARAMETER SkipConvert
    Reuse the CityJSON already generated for this IFC instead of reconverting.

.PARAMETER Overlay
    Also draw the source IFC in the same scene, for direct comparison.

.PARAMETER Semantic
    Only draw these semantic types, comma separated (e.g. WallSurface).

.PARAMETER Port
    Port to serve on. Default 8099.

.EXAMPLE
    .\scripts\view_cityjson.ps1

.EXAMPLE
    .\scripts\view_cityjson.ps1 -Semantic WallSurface

.EXAMPLE
    .\scripts\view_cityjson.ps1 -Overlay
#>
[CmdletBinding()]
param(
    [string]$Ifc,
    [string]$CityJson,
    [switch]$SkipConvert,
    [switch]$Overlay,
    [string]$Semantic,
    [int]$Port = 8099
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_viewer_common.ps1")

$root = Get-RepoRoot
Set-Location $root
$python = Get-PythonExe -Root $root

$ifcPath = $null
if ($CityJson) {
    if (-not (Test-Path $CityJson)) { throw "CityJSON file not found: $CityJson" }
}
else {
    $ifcPath = Select-IfcFile -Ifc $Ifc -Root $root
    $outDir = Join-Path "input" (Get-ModelSlug -Path $ifcPath)
    $CityJson = Join-Path $outDir "model.json"

    if ($SkipConvert) {
        if (-not (Test-Path $CityJson)) {
            throw "-SkipConvert was passed but $CityJson does not exist yet. Run once without it."
        }
        Write-Host "Reusing existing $CityJson" -ForegroundColor DarkGray
    }
    else {
        Write-Host ""
        Write-Host "== Stage 1+2: IFC -> LoD3 CityJSON ==" -ForegroundColor Yellow
        & $python (Join-Path "scripts" "ifc_to_cityjson.py") --ifc $ifcPath --out-dir $outDir
        if ($LASTEXITCODE -ne 0) { throw "ifc_to_cityjson.py failed (exit $LASTEXITCODE)" }

        Write-Host ""
        Write-Host "== Stage 3: sanitize (cjio) ==" -ForegroundColor Yellow
        & $python (Join-Path "scripts" "sanitize_cityjson.py") (Join-Path $outDir "model_raw.json") $CityJson
        if ($LASTEXITCODE -ne 0) { throw "sanitize_cityjson.py failed (exit $LASTEXITCODE)" }
    }
}

$glbName = "model_cityjson.glb"
$glb = Join-Path "docs" $glbName

Write-Host ""
Write-Host "== CityJSON -> GLB ==" -ForegroundColor Yellow

$argsList = @((Join-Path "scripts" "view_model.py"), "--cityjson", $CityJson)
if ($Overlay -and $ifcPath) { $argsList += @("--ifc", $ifcPath) }
if ($Semantic) { $argsList += @("--semantic", $Semantic) }
$argsList += @("--export-glb", $glb)

& $python $argsList
if ($LASTEXITCODE -ne 0) { throw "Export failed (exit $LASTEXITCODE)" }

$label = "IFC -> CityJSON (converted LoD3)"
if ($Overlay -and $ifcPath) { $label += " + raw IFC overlay" }
if ($Semantic) { $label += " [$Semantic]" }
Start-ModelViewer -Root $root -Python $python -GlbName $glbName -Label $label -Port $Port
