#!/usr/bin/env bash
# Deployment health checks — distinguishes "process alive" from
# "service actually usable".
#
#   1. FastAPI  GET  /health
#        200 {"status":"ok"}  = app up AND PostgreSQL reachable (usable)
#        503                  = app up but database unreachable (NOT usable)
#        no response          = process down
#   2. n8n      GET  /healthz   (the only health endpoint n8n 2.41.7 has)
#        200                  = process answering
#   3. End-to-end probe: POST {} at the production webhook (creates nothing —
#      validation fails first). Expected usable answer is HTTP 422 with
#      outcome=validation_failed, which proves webhook registered + workflow
#      published + FastAPI reachable in one shot.
#        404 = webhook not registered — retried for up to ~18 s by default
#              because registration lags /healthz on cold starts; if it
#              persists: run import-workflow.sh and restart n8n
#        502 = n8n fine, FastAPI unreachable (check lead-api / LEAD_API_BASE_URL)
#
# Tunables: HEALTH_WEBHOOK_RETRIES (default 6), HEALTH_WEBHOOK_INTERVAL (default 3s).
#
# Exit code: 0 = all usable, 1 = at least one check failed.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Pick up ports from the deployed env files when present, else defaults.
if [ -f "$REPO/deploy/env/api.env" ]; then set -a; . "$REPO/deploy/env/api.env"; set +a; fi
if [ -f "$REPO/deploy/env/n8n.env" ]; then set -a; . "$REPO/deploy/env/n8n.env"; set +a; fi

API_BASE="${API_BASE:-http://127.0.0.1:${API_PORT:-8000}}"
N8N_BASE="${N8N_BASE:-http://127.0.0.1:${N8N_PORT:-5678}}"
WEBHOOK_URL="${HEALTH_WEBHOOK_URL:-$N8N_BASE/webhook/lead-intake}"

FAIL=0
say() { printf '%s\n' "$*"; }
pass() { say "PASS  $*"; }
fail() { say "FAIL  $*"; FAIL=1; }

# --- 1. FastAPI ------------------------------------------------------
body="$(curl -s --max-time 5 "$API_BASE/health" 2>/dev/null || true)"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$API_BASE/health" 2>/dev/null || true)"
case "$code" in
  200) pass "api    /health 200 — app up, PostgreSQL reachable" ;;
  503) fail "api    /health 503 — app up but database NOT reachable ($body)" ;;
  *)   fail "api    /health unreachable (process down? code=$code)" ;;
esac

# --- 2. n8n ----------------------------------------------------------
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$N8N_BASE/healthz" 2>/dev/null || true)"
if [ "$code" = "200" ]; then
  pass "n8n    /healthz 200 — process answering"
else
  fail "n8n    /healthz unreachable (code=$code)"
fi

# --- 3. End-to-end webhook probe -------------------------------------
# Registration lags /healthz on cold starts (observed on fresh instances),
# so retry only the 404 case before declaring the chain unusable.
RETRIES="${HEALTH_WEBHOOK_RETRIES:-6}"
INTERVAL="${HEALTH_WEBHOOK_INTERVAL:-3}"
probe_file="$(mktemp)"
attempt=0
while :; do
  attempt=$((attempt + 1))
  probe="$(curl -s --max-time 15 -o "$probe_file" -w '%{http_code}' \
    -X POST -H 'Content-Type: application/json' \
    -d '{}' "$WEBHOOK_URL" 2>/dev/null || true)"
  probe_body="$(cat "$probe_file" 2>/dev/null || true)"
  if [ "$probe" = "404" ] && [ "$attempt" -lt "$RETRIES" ]; then
    say "wait  chain  webhook 404 — registration settling (attempt $attempt/$RETRIES)"
    sleep "$INTERVAL"
    continue
  fi
  break
done
rm -f "$probe_file"
case "$probe" in
  422)
    if printf '%s' "$probe_body" | grep -q 'validation_failed'; then
      pass "chain  POST $WEBHOOK_URL -> 422 validation_failed (webhook + workflow + api usable)"
    else
      fail "chain  POST webhook -> 422 but unexpected body: ${probe_body:0:160}"
    fi ;;
  404) fail "chain  webhook NOT registered — run deploy/bin/import-workflow.sh, restart n8n" ;;
  502) fail "chain  n8n ok but FastAPI unreachable (upstream_error) — check lead-api and LEAD_API_BASE_URL" ;;
  000) fail "chain  no answer from $WEBHOOK_URL" ;;
  *)   fail "chain  unexpected probe answer (code=$probe): ${probe_body:0:160}" ;;
esac

if [ "$FAIL" -eq 0 ]; then say "HEALTH OK — all checks usable"; else say "HEALTH FAILED"; fi
exit "$FAIL"
