# Shared helpers for view_ifc.ps1 and view_cityjson.ps1. Dot-source, don't run directly.

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Get-PythonExe {
    param([string]$Root)
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    Write-Warning "No .venv found at $venv - falling back to 'python' on PATH."
    return "python"
}

function Select-IfcFile {
    <#  Resolve an IFC path: use what was passed, else find the .ifc files in the repo
        and prompt when there's more than one. #>
    param([string]$Ifc, [string]$Root)

    if ($Ifc) {
        if (-not (Test-Path $Ifc)) { throw "IFC file not found: $Ifc" }
        return (Get-Item $Ifc).FullName
    }

    $candidates = @(
        Get-ChildItem -Path $Root -Filter *.ifc -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch '\\\.venv\\' -and $_.FullName -notmatch '\\node_modules\\' } |
            Sort-Object FullName
    )

    if ($candidates.Count -eq 0) {
        throw "No .ifc files found under $Root. Pass one with -Ifc <path>."
    }
    if ($candidates.Count -eq 1) {
        Write-Host "Using the only IFC found: $($candidates[0].Name)" -ForegroundColor Cyan
        return $candidates[0].FullName
    }

    Write-Host ""
    Write-Host "IFC files found:" -ForegroundColor Cyan
    for ($i = 0; $i -lt $candidates.Count; $i++) {
        $rel = $candidates[$i].FullName.Substring($Root.Length + 1)
        Write-Host ("  [{0}] {1}  ({2} MB)" -f ($i + 1), $rel, [math]::Round($candidates[$i].Length / 1MB, 1))
    }
    Write-Host ""
    $choice = Read-Host "Select [1-$($candidates.Count)]"
    $index = 0
    if (-not [int]::TryParse($choice, [ref]$index) -or $index -lt 1 -or $index -gt $candidates.Count) {
        throw "Invalid selection: '$choice'"
    }
    return $candidates[$index - 1].FullName
}

function Get-ModelSlug {
    param([string]$Path)
    $slug = [System.IO.Path]::GetFileNameWithoutExtension((Get-Item $Path).Name)
    $slug = [regex]::Replace($slug, '[^A-Za-z0-9]+', '_').Trim('_')
    if ($slug.Length -gt 48) { $slug = $slug.Substring(0, 48).Trim('_') }
    return $slug
}

function Start-ModelViewer {
    <#  Serve docs/ and open the viewer on a specific GLB, labelled so it's obvious
        which pipeline produced what you're looking at. #>
    param(
        [string]$Root,
        [string]$Python,
        [string]$GlbName,
        [string]$Label,
        [int]$Port
    )

    $url = "http://127.0.0.1:$Port/model.html?src=$([uri]::EscapeDataString($GlbName))&label=$([uri]::EscapeDataString($Label))"

    Write-Host ""
    Write-Host "== $Label ==" -ForegroundColor Green
    Write-Host "== Serving $url  (Ctrl+C to stop) ==" -ForegroundColor Yellow

    $server = Start-Process -FilePath $Python `
        -ArgumentList "-m", "http.server", "$Port", "-d", "docs" `
        -WorkingDirectory $Root -PassThru -NoNewWindow
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
}
