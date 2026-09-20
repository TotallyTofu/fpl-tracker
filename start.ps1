# FPL Team Optimizer - launcher (Windows)
#
#   .\start.ps1                   auto: prod if frontend\dist exists, else dev
#   .\start.ps1 -Prod             prod: single uvicorn on :8000 serving API + built frontend
#   .\start.ps1 -Dev              dev: API on :8000 + Vite dev server on :5173 (HMR)
#   .\start.ps1 -Prod -Port 9000  port override
#   .\start.ps1 -Prod -Rebuild    force `npm run build` even if dist exists
#
# Idempotent: venv / deps / build are created only when missing or stale.
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 mis-parses UTF-8
# no-BOM scripts that contain non-ASCII characters).

param(
    [switch]$Prod,
    [switch]$Dev,
    [int]$Port = 8000,
    [switch]$Rebuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "==> FPL Team Optimizer (root: $root)" -ForegroundColor Cyan

# --- Mode selection -------------------------------------------------------------
$distOk = Test-Path "frontend\dist\index.html"
if ($Prod -and $Dev) {
    Write-Host "Use either -Prod or -Dev, not both." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
if ($Prod) {
    $mode = "prod"
} elseif ($Dev) {
    $mode = "dev"
} elseif ($distOk) {
    $mode = "prod"
    Write-Host "==> frontend\dist exists -> prod mode (use -Dev for the Vite dev server)" -ForegroundColor DarkGray
} else {
    $mode = "dev"
    Write-Host "==> No frontend\dist -> dev mode (prod needs a build: -Prod -Rebuild)" -ForegroundColor DarkGray
}

# --- 1. Python venv + backend deps ----------------------------------------------
function Find-Python {
    $candidates = @(
        @("py", "-3.11"),
        @("py", "-3"),
        @("python"),
        @("python3")
    )
    foreach ($c in $candidates) {
        $exe = $c[0]
        $extra = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $ver = (& $exe @extra --version 2>&1 | Out-String)
            if ($LASTEXITCODE -eq 0 -and $ver -match "Python (\d+)\.(\d+)") {
                return @{ exe = $exe; args = $extra; version = "$($Matches[1]).$($Matches[2])" }
            }
        } catch { }
    }
    return $null
}

$pyvenv = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $pyvenv)) {
    $pyc = Find-Python
    if (-not $pyc) {
        Write-Host "Python 3.11+ not found. Install it: winget install Python.Python.3.11 (then reopen the terminal)." -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
    if ([version]$pyc.version -lt [version]"3.11") {
        Write-Host "WARNING: found Python $($pyc.version) - 3.11+ is recommended." -ForegroundColor Yellow
    }
    Write-Host "==> Creating Python venv with $($pyc.exe) $($pyc.args -join ' ') ($($pyc.version))..." -ForegroundColor Yellow
    $pyArgs = $pyc.args
    & $pyc.exe @pyArgs -m venv .venv
}
$py = $pyvenv

# Install backend deps only when requirements.txt changed since the last install.
$reqHash = (Get-FileHash "backend\requirements.txt" -Algorithm SHA256).Hash
$marker = Join-Path $root ".venv\.reqs.sha256"
if ((-not (Test-Path $marker)) -or ((Get-Content $marker -Raw).Trim() -ne $reqHash)) {
    Write-Host "==> Installing backend requirements (pip, no cache)..." -ForegroundColor Yellow
    & $py -m pip install --no-cache-dir -q -r backend\requirements.txt
    Set-Content -Path $marker -Value $reqHash -NoNewline
}

# --- 2. Frontend deps + build -----------------------------------------------------
$needNode = ($mode -eq "dev") -or (-not $distOk) -or $Rebuild
if ($needNode -and -not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host "Node.js not found. Install it: winget install OpenJS.NodeJS.LTS (then reopen the terminal)." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
if ($needNode) {
    if (-not (Test-Path "frontend\node_modules")) {
        Write-Host "==> Installing frontend dependencies (npm)..." -ForegroundColor Yellow
        Push-Location frontend
        try {
            npm install --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) {
                Write-Host "    npm install failed (exit $LASTEXITCODE); retrying with a project-local cache (restricted environments)..." -ForegroundColor Yellow
                npm install --no-audit --no-fund --cache (Join-Path $root ".npm-cache") --ignore-scripts
            }
        }
        finally { Pop-Location }
    }
    if ($mode -eq "dev" -or -not $distOk -or $Rebuild) {
        Write-Host "==> Building frontend (vite)..." -ForegroundColor Yellow
        Push-Location frontend
        try { npm run build }
        finally { Pop-Location }
    }
}

# --- 3. Run ------------------------------------------------------------------------
if ($mode -eq "prod") {
    Write-Host ""
    Write-Host "==> Prod mode: http://127.0.0.1:$Port  (Ctrl+C to stop)" -ForegroundColor Green
    try {
        & $py -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port $Port
        $code = $LASTEXITCODE
    } catch {
        Write-Host "Server crashed: $($_.Exception.Message)" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
    if ($code -ne 0) {
        Write-Host "Server exited with code $code." -ForegroundColor Red
        Read-Host "Press Enter to exit"
    }
    exit $code
} else {
    Write-Host ""
    Write-Host "==> Dev mode: API http://127.0.0.1:$Port  |  UI http://localhost:5173  (Ctrl+C to stop)" -ForegroundColor Green
    $env:FPL_API_TARGET = "http://127.0.0.1:$Port"
    $api = Start-Job -ScriptBlock {
        param($r, $p, $port)
        Set-Location (Join-Path $r "backend")
        & $p -m uvicorn app.main:app --host 127.0.0.1 --port $port
    } -ArgumentList $root, $py, $Port
    try {
        Push-Location frontend
        try { npx vite }
        finally { Pop-Location }
    }
    finally {
        Stop-Job $api
        Remove-Job $api -Force
    }
}