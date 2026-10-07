#!/usr/bin/env bash
# Start n8n deterministically (no shell-history magic).
# Used by systemd (deploy/systemd/lead-n8n.service) and deploy/bin/stack.sh.
# Environment (N8N_USER_FOLDER, N8N_*, LEAD_*, TELEGRAM_CHAT_ID) must
# already be in the process environment — stack.sh and the systemd unit
# both load deploy/env/n8n.env for you.
set -euo pipefail

N8N_BIN="${N8N_BIN:-$(command -v n8n || true)}"
if [ -z "$N8N_BIN" ]; then
  echo "lead-n8n: n8n not found on PATH (install the pinned version: npm install -g n8n@2.41.7)" >&2
  exit 1
fi

if [ -z "${N8N_USER_FOLDER:-}" ]; then
  echo "lead-n8n: N8N_USER_FOLDER is not set (copy deploy/env/n8n.env.example -> deploy/env/n8n.env)" >&2
  exit 1
fi

# Documents which version we are validated against; harmless otherwise.
if [ -n "${LEAD_N8N_EXPECT_VERSION:-}" ]; then
  ACTUAL="$("$N8N_BIN" --version 2>/dev/null | tr -d '[:space:]' || true)"
  if [ -n "$ACTUAL" ] && [ "$ACTUAL" != "$LEAD_N8N_EXPECT_VERSION" ]; then
    echo "lead-n8n: WARNING: running n8n $ACTUAL, deployment docs validate $LEAD_N8N_EXPECT_VERSION" >&2
  fi
fi

exec "$N8N_BIN" start
