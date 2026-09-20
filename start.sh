#!/usr/bin/env bash
# FPL Team Optimizer - launcher (Linux/macOS/WSL)
#
#   ./start.sh                   auto: prod if frontend/dist exists, else dev
#   ./start.sh --prod            prod: single uvicorn on :8000 serving API + built frontend
#   ./start.sh --dev             dev: API on :8000 + Vite dev server on :5173 (HMR)
#   ./start.sh --prod --port 9000   port override
#   ./start.sh --prod --rebuild     force `npm run build` even if dist exists
#
# Idempotent: venv / deps / build are created only when missing or stale.
set -euo pipefail
cd "$(dirname "$0")"

MODE="" PORT=8000 REBUILD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prod) MODE=prod; shift ;;
    --dev) MODE=dev; shift ;;
    --port) PORT="${2:?--port needs a value}"; shift 2 ;;
    --rebuild) REBUILD=1; shift ;;
    *) echo "Unknown argument: $1 (use --prod / --dev / --port N / --rebuild)" >&2; exit 1 ;;
  esac
done

echo "==> FPL Team Optimizer (root: $(pwd))"

dist_ok=0
if [[ -f frontend/dist/index.html ]]; then dist_ok=1; fi
if [[ -z $MODE ]]; then
  if [[ $dist_ok -eq 1 ]]; then
    MODE=prod
    echo "==> frontend/dist exists -> prod mode (use --dev for the Vite dev server)"
  else
    MODE=dev
    echo "==> No frontend/dist -> dev mode (prod needs a build: --prod --rebuild)"
  fi
fi

# --- 1. Python venv + backend deps ----------------------------------------------
PYEXE=""
for c in python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PYEXE="$c"; break; fi
done
if [[ -z $PYEXE ]]; then
  echo "Python 3 not found. Install Python 3.11+ (e.g. apt install python3.11 / brew install python@3.11)." >&2
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  echo "==> Creating Python venv with $PYEXE..."
  "$PYEXE" -m venv .venv
fi
PY=.venv/bin/python

# Install backend deps only when requirements.txt changed since the last install.
if command -v sha256sum >/dev/null 2>&1; then
  req_hash=$(sha256sum backend/requirements.txt | awk '{print $1}')
else
  req_hash=$(shasum -a 256 backend/requirements.txt | awk '{print $1}')
fi
marker=.venv/.reqs.sha256
if [[ ! -f $marker ]] || [[ "$(cat "$marker" 2>/dev/null || true)" != "$req_hash" ]]; then
  echo "==> Installing backend requirements (pip, no cache)..."
  "$PY" -m pip install --no-cache-dir -q -r backend/requirements.txt
  printf '%s' "$req_hash" > "$marker"
fi

# --- 2. Frontend deps + build ------------------------------------------------------
need_node=0
if [[ $MODE == dev || $dist_ok -eq 0 || $REBUILD -eq 1 ]]; then need_node=1; fi
if [[ $need_node -eq 1 ]] && ! command -v node >/dev/null 2>&1; then
  echo "Node.js not found. Install Node 18+ (e.g. apt install nodejs npm / brew install node)." >&2
  exit 1
fi
if [[ $need_node -eq 1 ]]; then
  if [[ ! -d frontend/node_modules ]]; then
    echo "==> Installing frontend dependencies (npm)..."
    (cd frontend && npm install --no-audit --no-fund) || {
      echo "    npm install failed; retrying with a project-local cache (restricted environments)..."
      (cd frontend && npm install --no-audit --no-fund --cache "$PWD/.npm-cache" --ignore-scripts)
    }
  fi
  if [[ $MODE == dev || $dist_ok -eq 0 || $REBUILD -eq 1 ]]; then
    echo "==> Building frontend (vite)..."
    (cd frontend && npm run build)
  fi
fi

# --- 3. Run --------------------------------------------------------------------------
if [[ $MODE == prod ]]; then
  echo ""
  echo "==> Prod mode: http://127.0.0.1:$PORT  (Ctrl+C to stop)"
  exec "$PY" -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port "$PORT"
else
  echo ""
  echo "==> Dev mode: API http://127.0.0.1:$PORT  |  UI http://localhost:5173  (Ctrl+C to stop)"
  export FPL_API_TARGET="http://127.0.0.1:$PORT"
  (cd backend && "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT") &
  API_PID=$!
  trap 'kill "$API_PID" 2>/dev/null || true' EXIT
  (cd frontend && npx vite)
fi