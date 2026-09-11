<#
.SYNOPSIS
    IFC -> GLB -> browser. Raw geometry, NO CityJSON conversion, NO panels.

.DESCRIPTION
    Reads the IFC with ifcopenshell and exports it straight to docs/model_ifc.glb, then
    serves it. This is the unprocessed source: both faces of every wall, interior walls
    included, coloured by IFC class. Use it as ground truth when checking the conversion.

    For the converted LoD3 CityJSON instead, use view_cityjson.ps1.

.PARAMETER Ifc
    IFC file to display. Omit to pick from the .ifc files found in the repo.

.PARAMETER Classes
    IFC classes to include, comma separated. Default is the envelope set
    (IfcWall, IfcRoof, IfcSlab, IfcWindow, IfcDoor); pass "all" for every product.

.PARAMETER ExteriorOnly
    Apply the converter's exterior classification, so you see only the walls/slabs the
    pipeline treats as envelope.

.PARAMETER Port
    Port to serve on. Default 8099.

.EXAMPLE
    .\scripts\view_ifc.ps1

.EXAMPLE
    .\scripts\view_ifc.ps1 -Classes all
#>
[CmdletBinding()]
param(
    [string]$Ifc,
    [string]$Classes,
    [switch]$ExteriorOnly,
    [int]$Port = 8099
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_viewer_common.ps1")

$root = Get-RepoRoot
Set-Location $root
$python = Get-PythonExe -Root $root

$ifcPath = Select-IfcFile -Ifc $Ifc -Root $root
$glbName = "model_ifc.glb"
$glb = Join-Path "docs" $glbName

Write-Host ""
Write-Host "== IFC -> GLB (raw, no conversion) ==" -ForegroundColor Yellow
Write-Host "   source: $(Split-Path $ifcPath -Leaf)"

$argsList = @((Join-Path "scripts" "view_model.py"), "--ifc", $ifcPath)
if ($Classes) { $argsList += @("--classes", $Classes) }
if ($ExteriorOnly) { $argsList += "--exterior-only" }
$argsList += @("--export-glb", $glb)

& $python $argsList
if ($LASTEXITCODE -ne 0) { throw "Export failed (exit $LASTEXITCODE)" }

$label = "Raw IFC - no conversion"
if ($ExteriorOnly) { $label += " (exterior only)" }
Start-ModelViewer -Root $root -Python $python -GlbName $glbName -Label $label -Port $Port
