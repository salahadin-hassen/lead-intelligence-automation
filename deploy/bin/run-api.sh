#!/usr/bin/env bash
# Start the FastAPI lead API deterministically (uvicorn on loopback).
# Used by systemd (deploy/systemd/lead-api.service) and deploy/bin/stack.sh.
# Environment (DATABASE_URL, API_HOST/API_PORT, optional OpenAI vars) must
# already be in the process environment — stack.sh and the systemd unit
# both load deploy/env/api.env for you.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

PYTHON="${LEAD_API_PYTHON:-$REPO/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || true)"
fi
if [ -z "$PYTHON" ]; then
  echo "lead-api: no python3 found (create .venv or set LEAD_API_PYTHON)" >&2
  exit 1
fi

if [ -z "${DATABASE_URL:-}" ]; then
  echo "lead-api: DATABASE_URL is not set (copy deploy/env/api.env.example -> deploy/env/api.env)" >&2
  exit 1
fi

exec "$PYTHON" -m uvicorn app.main:app \
  --host "${API_HOST:-127.0.0.1}" \
  --port "${API_PORT:-8000}"
