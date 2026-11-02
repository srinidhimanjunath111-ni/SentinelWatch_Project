#!/usr/bin/env bash
# Create the virtual environment if needed, install requirements, and start SentinelWatch.
# PyTorch is installed so the trained models run; pass --no-models to skip it, in which case
# the pipeline uses its labelled spectral baseline.
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-8000}"
PYTHON="${PYTHON:-python3}"

if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv"
  "$PYTHON" -m venv .venv
fi

STAMP=.venv/.requirements-installed
if [ ! -f "$STAMP" ] || [ requirements-operational.txt -nt "$STAMP" ] || [ requirements-core.txt -nt "$STAMP" ]; then
  echo "Installing requirements"
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements-operational.txt
  touch "$STAMP"
fi

if [ "${1:-}" != "--no-models" ] && ! .venv/bin/python -c "import torch" 2>/dev/null; then
  echo "Installing PyTorch"
  .venv/bin/python -m pip install --quiet torch \
    || echo "PyTorch could not be installed; continuing with the spectral baseline."
fi

echo "Starting SentinelWatch on http://127.0.0.1:${PORT}"
exec .venv/bin/python -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "$PORT" --timeout-graceful-shutdown 5
