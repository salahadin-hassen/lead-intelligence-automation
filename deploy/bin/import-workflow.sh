#!/usr/bin/env bash
# Import + publish the committed workflow into $N8N_USER_FOLDER.
# Idempotent (import upserts by workflow id "lead-intake").
#
# Runs OFFLINE: it writes n8n's database directly, so run it while n8n is
# stopped (or before the first start), then (re)start n8n so the
# production webhook registers. stack.sh restart applies it for you
# (stack.sh start also waits for n8n's asynchronous publication outbox
# to converge, so the restarted instance runs THIS content).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WF="$REPO/n8n/lead-intake.json"
N8N_ENV="${N8N_ENV_FILE:-$REPO/deploy/env/n8n.env}"

# Self-load the n8n env file when the caller has not exported the values
# (stack.sh exports them for its children; a manual run should also work).
if [ -z "${N8N_USER_FOLDER:-}" ] && [ -f "$N8N_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$N8N_ENV"
  set +a
fi

if [ -z "${N8N_USER_FOLDER:-}" ]; then
  echo "import-workflow: N8N_USER_FOLDER is not set (copy deploy/env/n8n.env.example -> deploy/env/n8n.env)" >&2
  exit 1
fi
N8N_BIN="${N8N_BIN:-$(command -v n8n || true)}"
if [ -z "$N8N_BIN" ]; then
  echo "import-workflow: n8n not found on PATH (npm install -g n8n@2.41.7)" >&2
  exit 1
fi
[ -f "$WF" ] || { echo "import-workflow: missing $WF" >&2; exit 1; }

"$N8N_BIN" import:workflow --input="$WF"
"$N8N_BIN" publish:workflow --id=lead-intake
echo "import-workflow: lead-intake imported + published into $N8N_USER_FOLDER/.n8n"
