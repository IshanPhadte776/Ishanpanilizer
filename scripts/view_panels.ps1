<#
.SYNOPSIS
    Panelize the existing CityJSON and open the panel viewer. Does NOT re-run the IFC conversion.

.DESCRIPTION
    Stage 4 on its own: takes the LoD3 CityJSON already on disk, runs the panelizer over its
    WallSurfaces, and serves the panel viewer (docs/index.html) with the layout and cost
    summary. Use this once the conversion is good and you're iterating on panel sizing.

    To regenerate the CityJSON from the IFC first, use view_cityjson.ps1 or run_ifc_demo.ps1.

.PARAMETER CityJson
    CityJSON to panelize. Defaults to whatever config/panelizer_config.json points at.

.PARAMETER PanelWidth
    Panel width in meters (config default is 1.2).

.PARAMETER PanelHeight
    Panel height in meters (config default is 2.4).

.PARAMETER Port
    Port to serve on. Default 8098.

.PARAMETER NoServe
    Panelize and write the outputs, but don't start a server.

.EXAMPLE
    .\scripts\view_panels.ps1

.EXAMPLE
    .\scripts\view_panels.ps1 -PanelWidth 1.5 -PanelHeight 3.0
#>
[CmdletBinding()]
param(
    [string]$CityJson,
    [double]$PanelWidth = 0,
    [double]$PanelHeight = 0,
    [int]$Port = 8098,
    [switch]$NoServe
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_viewer_common.ps1")

$root = Get-RepoRoot
Set-Location $root
$python = Get-PythonExe -Root $root

if ($CityJson -and -not (Test-Path $CityJson)) {
    throw "CityJSON file not found: $CityJson"
}

# Point the config at the requested model / panel size. Done in Python rather than
# ConvertTo-Json: Windows PowerShell 5.1 defaults to -Depth 2 and would silently mangle
# the nested arrays in this config.
# "-" is a sentinel for "not supplied": PowerShell drops empty strings from an argument
# array entirely, which would shift every remaining positional argument.
$configUpdate = @"
import json, sys
p = 'config/panelizer_config.json'
with open(p) as fh:
    cfg = json.load(fh)
if sys.argv[1] != '-':
    cfg['input_json'] = sys.argv[1].replace('\\', '/')
if float(sys.argv[2]) > 0:
    cfg['panel_width'] = float(sys.argv[2])
if float(sys.argv[3]) > 0:
    cfg['panel_height'] = float(sys.argv[3])
with open(p, 'w') as fh:
    json.dump(cfg, fh, indent=2)
print('input : %s' % cfg['input_json'])
print('panel : %s x %s m' % (cfg['panel_width'], cfg['panel_height']))
"@

$cityJsonArg = if ($CityJson) { $CityJson } else { "-" }

Write-Host ""
Write-Host "== Panelizing existing CityJSON (no IFC re-conversion) ==" -ForegroundColor Yellow
& $python -c $configUpdate $cityJsonArg $PanelWidth $PanelHeight
if ($LASTEXITCODE -ne 0) { throw "Failed to update config/panelizer_config.json" }

Write-Host ""
& $python (Join-Path $root "main.py") --config (Join-Path "config" "panelizer_config.json")
if ($LASTEXITCODE -ne 0) { throw "main.py failed (exit $LASTEXITCODE)" }

Write-Host ""
Write-Host "== Building panel viewer payloads ==" -ForegroundColor Yellow
Write-Host "   (overwrites docs/demo_*.json; git checkout docs/ restores the bundled demo)" -ForegroundColor DarkGray
& $python (Join-Path "scripts" "export_docs_demo.py")
if ($LASTEXITCODE -ne 0) { throw "export_docs_demo.py failed (exit $LASTEXITCODE)" }

if ($NoServe) {
    Write-Host ""
    Write-Host "Done (-NoServe: not starting a server)." -ForegroundColor Green
    return
}

$url = "http://127.0.0.1:$Port/index.html"
Write-Host ""
Write-Host "== Panel layout ==" -ForegroundColor Green
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
