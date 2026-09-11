<#
.SYNOPSIS
    One command: pick an IFC file, run the full IFC -> CityJSON -> panels pipeline, and open the viewer.

.DESCRIPTION
    Chains the four pipeline stages and then serves the result:
      1+2. scripts/ifc_to_cityjson.py   IFC envelope -> LoD3 CityJSON (model_raw.json)
      3.   scripts/sanitize_cityjson.py cjio vertices_clean + structural checks (model.json)
      4.   main.py                      panelize -> output/<name>_panels.json
           scripts/export_docs_demo.py  regenerate docs/demo_*.json for the viewer

    With no -Ifc argument it finds every .ifc in the repo and prompts you to pick one.

.PARAMETER Ifc
    Path to the .ifc file. Omit to choose interactively from the .ifc files found in the repo.

.PARAMETER OutDir
    Where the CityJSON lands. Defaults to input/<sanitized ifc name>/.

.PARAMETER PanelWidth
    Override panel width in meters (config default is 1.2).

.PARAMETER PanelHeight
    Override panel height in meters (config default is 2.4).

.PARAMETER Port
    Port for the static viewer. Default 8098 (8080 is taken by a local Apache on this machine).

.PARAMETER SkipExtract
    Reuse the existing CityJSON for this IFC and just re-panelize + re-serve.

.PARAMETER Live
    Launch the FastAPI backend + React UI (scripts/start_all.ps1) instead of the static viewer.

.PARAMETER NoServe
    Run the pipeline but don't start a server.

.EXAMPLE
    .\scripts\run_ifc_demo.ps1
    Pick an IFC interactively, run everything, open the viewer.

.EXAMPLE
    .\scripts\run_ifc_demo.ps1 -Ifc "COLONEL BY CHILD CARE CENTRE_ifc4_2025_full.ifc" -PanelWidth 1.5 -PanelHeight 3.0
#>
[CmdletBinding()]
param(
    [string]$Ifc,
    [string]$OutDir,
    [double]$PanelWidth = 0,
    [double]$PanelHeight = 0,
    [int]$Port = 8098,
    [switch]$SkipExtract,
    [switch]$Live,
    [switch]$NoServe
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Warning "No .venv found at $python - falling back to 'python' on PATH."
    $python = "python"
}

# ---------------------------------------------------------------- pick the IFC

if (-not $Ifc) {
    $candidates = @(
        Get-ChildItem -Path $root -Filter *.ifc -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch '\\\.venv\\' -and $_.FullName -notmatch '\\node_modules\\' } |
            Sort-Object FullName
    )

    if ($candidates.Count -eq 0) {
        throw "No .ifc files found under $root. Pass one explicitly with -Ifc <path>."
    }
    elseif ($candidates.Count -eq 1) {
        $Ifc = $candidates[0].FullName
        Write-Host "Using the only IFC found: $($candidates[0].Name)" -ForegroundColor Cyan
    }
    else {
        Write-Host ""
        Write-Host "IFC files found in this repo:" -ForegroundColor Cyan
        for ($i = 0; $i -lt $candidates.Count; $i++) {
            $rel = $candidates[$i].FullName.Substring($root.Length + 1)
            $mb = [math]::Round($candidates[$i].Length / 1MB, 1)
            Write-Host ("  [{0}] {1}  ({2} MB)" -f ($i + 1), $rel, $mb)
        }
        Write-Host ""
        $choice = Read-Host "Select an IFC [1-$($candidates.Count)]"
        $index = 0
        if (-not [int]::TryParse($choice, [ref]$index) -or $index -lt 1 -or $index -gt $candidates.Count) {
            throw "Invalid selection: '$choice'"
        }
        $Ifc = $candidates[$index - 1].FullName
    }
}

if (-not (Test-Path $Ifc)) {
    throw "IFC file not found: $Ifc"
}
$ifcItem = Get-Item $Ifc
$ifcPath = $ifcItem.FullName

if (-not $OutDir) {
    $slug = [System.IO.Path]::GetFileNameWithoutExtension($ifcItem.Name)
    $slug = [regex]::Replace($slug, '[^A-Za-z0-9]+', '_').Trim('_')
    if ($slug.Length -gt 48) { $slug = $slug.Substring(0, 48).Trim('_') }
    $OutDir = Join-Path "input" $slug
}

$modelRaw = Join-Path $OutDir "model_raw.json"
$modelJson = Join-Path $OutDir "model.json"
$panelsName = (Split-Path $OutDir -Leaf) + "_panels.json"
$outputJson = Join-Path "output" $panelsName

Write-Host ""
Write-Host "IFC    : $($ifcItem.Name)" -ForegroundColor Green
Write-Host "Model  : $modelJson" -ForegroundColor Green
Write-Host "Panels : $outputJson" -ForegroundColor Green
Write-Host ""

# ------------------------------------------------------------- stages 1+2 / 3

if ($SkipExtract) {
    if (-not (Test-Path $modelJson)) {
        throw "-SkipExtract was passed but $modelJson does not exist yet. Run once without it."
    }
    Write-Host "== Stages 1-3 skipped, reusing $modelJson ==" -ForegroundColor DarkGray
}
else {
    Write-Host "== Stage 1+2: IFC -> LoD3 CityJSON ==" -ForegroundColor Yellow
    & $python (Join-Path "scripts" "ifc_to_cityjson.py") --ifc $ifcPath --out-dir $OutDir
    if ($LASTEXITCODE -ne 0) { throw "ifc_to_cityjson.py failed (exit $LASTEXITCODE)" }

    Write-Host ""
    Write-Host "== Stage 3: sanitize (cjio) ==" -ForegroundColor Yellow
    & $python (Join-Path "scripts" "sanitize_cityjson.py") $modelRaw $modelJson
    if ($LASTEXITCODE -ne 0) { throw "sanitize_cityjson.py failed (exit $LASTEXITCODE)" }
}

# --------------------------------------------------------------- point config

# Done in Python rather than ConvertTo-Json: Windows PowerShell 5.1 defaults to
# -Depth 2 and would silently mangle the nested arrays in this config.
$configUpdate = @"
import json, sys
p = 'config/panelizer_config.json'
with open(p) as fh:
    cfg = json.load(fh)
cfg['input_json'] = sys.argv[1].replace('\\', '/')
cfg['output_json'] = sys.argv[2].replace('\\', '/')
if float(sys.argv[3]) > 0:
    cfg['panel_width'] = float(sys.argv[3])
if float(sys.argv[4]) > 0:
    cfg['panel_height'] = float(sys.argv[4])
with open(p, 'w') as fh:
    json.dump(cfg, fh, indent=2)
print('config -> input_json=%s panel=%sx%s' % (cfg['input_json'], cfg['panel_width'], cfg['panel_height']))
"@

Write-Host ""
Write-Host "== Pointing config/panelizer_config.json at this model ==" -ForegroundColor Yellow
& $python -c $configUpdate $modelJson $outputJson $PanelWidth $PanelHeight
if ($LASTEXITCODE -ne 0) { throw "Failed to update config/panelizer_config.json" }

# ------------------------------------------------------------------ stage 4

Write-Host ""
Write-Host "== Stage 4: panelize ==" -ForegroundColor Yellow
& $python (Join-Path $root "main.py") --config (Join-Path "config" "panelizer_config.json")
if ($LASTEXITCODE -ne 0) { throw "main.py failed (exit $LASTEXITCODE)" }

Write-Host ""
Write-Host "== Regenerating viewer payloads (docs/demo_*.json) ==" -ForegroundColor Yellow
Write-Host "   (git checkout docs/ restores the original bundled demo)" -ForegroundColor DarkGray
& $python (Join-Path "scripts" "export_docs_demo.py")
if ($LASTEXITCODE -ne 0) { throw "export_docs_demo.py failed (exit $LASTEXITCODE)" }

if ($NoServe) {
    Write-Host ""
    Write-Host "Done (-NoServe: not starting a server)." -ForegroundColor Green
    return
}

# --------------------------------------------------------------------- serve

if ($Live) {
    Write-Host ""
    Write-Host "== Starting backend + UI ==" -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot "start_all.ps1")
    return
}

$url = "http://127.0.0.1:$Port"
Write-Host ""
Write-Host "== Serving viewer at $url (Ctrl+C to stop) ==" -ForegroundColor Yellow

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
