# FPL Team Optimizer — launcher (Windows)
#
#   .\start.ps1          dev mode: API on :8000 + Vite dev server on :5173 (UI)
#   .\start.ps1 -Prod    prod mode: API on :8000 serving the built frontend
#
# Idempotent: creates the venv / installs deps / builds the frontend only if missing.

param([switch]$Prod)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "==> FPL Team Optimizer (root: $root)" -ForegroundColor Cyan

# --- 1. Python venv + backend deps -------------------------------------------
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "==> Creating Python venv..." -ForegroundColor Yellow
    python -m venv .venv
}
Write-Host "==> Installing backend requirements (pip, no cache)..." -ForegroundColor Yellow
& $py -m pip install --no-cache-dir -q -r backend\requirements.txt

# --- 2. Frontend deps + build -------------------------------------------------
if (-not (Test-Path "frontend\node_modules")) {
    Write-Host "==> Installing frontend dependencies (npm)..." -ForegroundColor Yellow
    Push-Location frontend
    try { npm install --no-audit --no-fund }
    finally { Pop-Location }
}
if (-not (Test-Path "frontend\dist\index.html")) {
    Write-Host "==> Building frontend (vite)..." -ForegroundColor Yellow
    Push-Location frontend
    try { npx vite build }
    finally { Pop-Location }
}

# --- 3. Run -------------------------------------------------------------------
if ($Prod) {
    Write-Host ""
    Write-Host "==> Prod mode: http://127.0.0.1:8000  (Ctrl+C to stop)" -ForegroundColor Green
    & $py -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
}
else {
    Write-Host ""
    Write-Host "==> Dev mode: API http://127.0.0.1:8000  |  UI http://localhost:5173  (Ctrl+C to stop)" -ForegroundColor Green
    $api = Start-Job -ScriptBlock {
        param($r, $p)
        Set-Location (Join-Path $r "backend")
        & $p -m uvicorn app.main:app --host 127.0.0.1 --port 8000
    } -ArgumentList $root, $py
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