#!/usr/bin/env bash
# Apply sql/*.sql migrations explicitly ("apply migrations" step).
#
# This uses the application's OWN migration mechanism (app.db.init_schema:
# sorted, idempotent sql files) — there is no second migration system.
# The API also applies the same migrations automatically at startup, so
# running the API against a fresh database already migrates it; this
# script exists for explicit runs (e.g. before a cutover).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

ENV_FILE="${API_ENV_FILE:-$REPO/deploy/env/api.env}"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi

if [ -z "${DATABASE_URL:-}" ]; then
  echo "migrate: DATABASE_URL is not set (cp $ENV_FILE.example $ENV_FILE and edit)" >&2
  exit 1
fi

PYTHON="${LEAD_API_PYTHON:-$REPO/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then PYTHON="$(command -v python3)"; fi

"$PYTHON" - <<'PY'
import os
from app import db

db.configure_pool(os.environ["DATABASE_URL"])
db.init_schema()
db.close_pool()
print("migrations applied (idempotent)")
PY
