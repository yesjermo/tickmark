#!/usr/bin/env bash
# One-command local demo. No API keys needed.
#   ./run.sh        build the UI once, serve everything on http://localhost:8000
#   ./run.sh dev    API on :8000 + Vite dev server with hot reload on :5173
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

cd "$ROOT/backend"
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null; then uv venv -q .venv && uv pip install -q --python .venv/bin/python -r requirements.txt
  else python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; fi
fi
cd "$ROOT/frontend"
[ -d node_modules ] || npm install --silent

if [ "${1:-}" = "dev" ]; then
  (cd "$ROOT/backend" && .venv/bin/uvicorn app.main:app --port 8000) &
  trap 'kill %1' EXIT
  npm run dev
else
  npm run build >/dev/null
  echo "SwingDesk on http://localhost:8000  (paper trading, simulated data)"
  cd "$ROOT/backend" && exec .venv/bin/uvicorn app.main:app --port 8000
fi
