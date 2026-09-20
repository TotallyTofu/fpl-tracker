#!/usr/bin/env bash
# FPL Team Optimizer — launcher (Linux/macOS/WSL)
#
#   ./start.sh           dev mode: API on :8000 + Vite dev server on :5173 (UI)
#   ./start.sh --prod    prod mode: API on :8000 serving the built frontend
#
# Idempotent: creates the venv / installs deps / builds the frontend only if missing.
set -euo pipefail
cd "$(dirname "$0")"

PROD=0
[[ "${1:-}" == "--prod" ]] && PROD=1

echo "==> FPL Team Optimizer (root: $(pwd))"

# --- 1. Python venv + backend deps --------------------------------------------
if [[ ! -x .venv/bin/python ]]; then
    echo "==> Creating Python venv..."
    python3 -m venv .venv
fi
PY=.venv/bin/python
echo "==> Installing backend requirements (pip, no cache)..."
"$PY" -m pip install --no-cache-dir -q -r backend/requirements.txt

# --- 2. Frontend deps + build ---------------------------------------------------
if [[ ! -d frontend/node_modules ]]; then
    echo "==> Installing frontend dependencies (npm)..."
    (cd frontend && npm install --no-audit --no-fund)
fi
if [[ ! -f frontend/dist/index.html ]]; then
    echo "==> Building frontend (vite)..."
    (cd frontend && npx vite build)
fi

# --- 3. Run ---------------------------------------------------------------------
if [[ $PROD -eq 1 ]]; then
    echo ""
    echo "==> Prod mode: http://127.0.0.1:8000  (Ctrl+C to stop)"
    exec "$PY" -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
else
    echo ""
    echo "==> Dev mode: API http://127.0.0.1:8000  |  UI http://localhost:5173  (Ctrl+C to stop)"
    (cd backend && "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8000) &
    API_PID=$!
    trap 'kill "$API_PID" 2>/dev/null || true' EXIT
    (cd frontend && npx vite)
fi