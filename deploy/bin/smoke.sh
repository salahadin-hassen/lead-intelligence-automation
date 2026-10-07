#!/usr/bin/env bash
# End-to-end smoke test against a RUNNING stack, through the real path
# (browser-substitute POST -> n8n webhook -> FastAPI -> PostgreSQL ->
# qualification -> n8n execution data -> Telegram).
#
# Cases:
#   0. health checks must pass first (deploy/bin/healthcheck.sh)
#   1. not-qualified lead -> 201 outcome=created, row persisted,
#                            n8n took the "Log Normal Route" branch
#   2. qualified lead     -> 201, row persisted, n8n "Log Priority Route"
#                            AND Telegram answered ok:true (message_id)
#   3. duplicate          -> same external_id again -> 409 outcome=duplicate,
#                            still exactly one row
#   4. invalid payload    -> 422 outcome=validation_failed, no new row
#   5. cleanup            -> removes this run's rows (skip with --keep)
#
# Notes:
#   * Sends ONE real Telegram notification per run (case 2).
#   * Non-destructive otherwise; only touches leads whose name starts
#     with this run's marker.
#   * Default webhook is the local n8n origin; set SMOKE_WEBHOOK_URL to
#     aim it at the reverse proxy instead (e.g. https://host/webhook/lead-intake).
set -u -o pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ "${1:-}" = "--keep" ]; then KEEP=1; else KEEP=0; fi

if [ -f "$REPO/deploy/env/api.env" ]; then set -a; . "$REPO/deploy/env/api.env"; set +a; fi
if [ -f "$REPO/deploy/env/n8n.env" ]; then set -a; . "$REPO/deploy/env/n8n.env"; set +a; fi

API_BASE="${SMOKE_API_BASE:-http://127.0.0.1:${API_PORT:-8000}}"
WEBHOOK="${SMOKE_WEBHOOK_URL:-http://127.0.0.1:${N8N_PORT:-5678}/webhook/lead-intake}"
SOURCE="website-contact-form"
RUN="M12 smoke $(date +%s)-$$"
BODY="$(mktemp)"
trap 'rm -f "$BODY"' EXIT

PASSED=0
FAILED=0
result() { # result <exit-code 0=ok> <name> [info]
  if [ "$1" = "0" ]; then
    echo "PASS  $2${3:+ — $3}"; PASSED=$((PASSED + 1))
  else
    echo "FAIL  $2${3:+ — $3}"; FAILED=$((FAILED + 1))
  fi
}

post() { # post <payload-json> -> sets CODE (000 when unreachable)
  CODE="$(curl -s -o "$BODY" -w '%{http_code}' --max-time 30 \
    -X POST -H 'Content-Type: application/json' -d "$1" "$WEBHOOK" 2>/dev/null)"
}
field() { # field <json-path e.g. outcome> -> value or empty
  python3 -c 'import json,sys
try:
    d = json.load(open(sys.argv[1]))
    for k in sys.argv[2].split("."):
        d = d[k]
    print(d)
except Exception:
    print("")' "$BODY" "$1" 2>/dev/null
}
count_rows() { # count_rows <name-prefix>
  curl -s --max-time 10 "$API_BASE/leads?source=$SOURCE&limit=100" 2>/dev/null |
    python3 -c 'import json,sys
try:
    items = json.load(sys.stdin).get("items", [])
    print(sum(1 for i in items if str(i.get("name") or "").startswith(sys.argv[1])))
except Exception:
    print(-1)' "$1" 2>/dev/null
}
latest_exec() {
  "$REPO/deploy/bin/n8n_exec.py" list 2>/dev/null |
    sed -n '1s/^exec \([0-9][0-9]*\).*/\1/p'
}
n8n_search() { # n8n_search <after> <timeout> <pattern...>
  local after="$1" timeout="$2"
  shift 2
  N8N_USER_FOLDER="${N8N_USER_FOLDER:-}" "$REPO/deploy/bin/n8n_exec.py" search "$@" \
    --after "$after" --timeout "$timeout"
}

echo "smoke: webhook=$WEBHOOK api=$API_BASE marker=\"$RUN\""

# --- 0. health -----------------------------------------------------------
if ! "$REPO/deploy/bin/healthcheck.sh"; then
  echo "smoke: stack is not healthy — fix health first" >&2
  exit 1
fi

# --- 1. not-qualified ----------------------------------------------------
MSG_NQ='Quick question.'
PAYLOAD_NQ=$(python3 -c 'import json,sys; print(json.dumps({
  "name": sys.argv[1], "email": "jenny.doe@gmail.com", "company": "Small biz",
  "message": sys.argv[2], "source": sys.argv[3], "external_id": sys.argv[4]}))' \
  "$RUN N" "$MSG_NQ" "$SOURCE" "smoke-nq-$$-$RANDOM")
PRE="$(latest_exec)"
post "$PAYLOAD_NQ"
OUTCOME="$(field outcome)"
[ "$CODE" = "201" ] && [ "$OUTCOME" = "created" ]
result $? "not-qualified 201/created" "code=$CODE outcome=$OUTCOME"
[ "$(count_rows "$RUN N")" = "1" ]
result $? "not-qualified row persisted"
n8n_search "${PRE:-0}" 30 '"Log Normal Route"' >/dev/null 2>&1
result $? "not-qualified n8n branch (Log Normal Route)"

# --- 2. qualified (+ Telegram delivery) ----------------------------------
MSG_Q="We're evaluating tools for our sales team and would like an enterprise plan. We need pricing details, an API integration with our CRM, and a live demo before a trial next month."
EXT_Q="smoke-q-$$-$RANDOM"
PAYLOAD_Q=$(python3 -c 'import json,sys; print(json.dumps({
  "name": sys.argv[1], "email": "m.webb@northwind-logistics.com",
  "company": "Northwind Logistics", "message": sys.argv[2],
  "source": sys.argv[3], "external_id": sys.argv[4]}))' \
  "$RUN Q" "$MSG_Q" "$SOURCE" "$EXT_Q")
PRE="$(latest_exec)"
post "$PAYLOAD_Q"
OUTCOME="$(field outcome)"
[ "$CODE" = "201" ] && [ "$OUTCOME" = "created" ]
result $? "qualified 201/created" "code=$CODE outcome=$OUTCOME"
[ "$(count_rows "$RUN Q")" = "1" ]
result $? "qualified row persisted"
n8n_search "${PRE:-0}" 45 '"Log Priority Route"' '"ok": true' '"message_id":' >/dev/null 2>&1
result $? "qualified n8n branch + Telegram ok:true"

# --- 3. duplicate --------------------------------------------------------
PRE="$(latest_exec)"
post "$PAYLOAD_Q"
OUTCOME="$(field outcome)"
[ "$CODE" = "409" ] && [ "$OUTCOME" = "duplicate" ]
result $? "duplicate 409" "code=$CODE outcome=$OUTCOME"
[ "$(count_rows "$RUN Q")" = "1" ]
result $? "duplicate created no second row"

# --- 4. validation -------------------------------------------------------
BEFORE_ROWS="$(count_rows "$RUN")"
PAYLOAD_BAD=$(python3 -c 'import json,sys; print(json.dumps({
  "name": sys.argv[1], "email": "not-an-email", "company": "Bad Co",
  "message": "hello", "source": sys.argv[2], "external_id": sys.argv[3]}))' \
  "$RUN BAD" "$SOURCE" "smoke-bad-$$-$RANDOM")
post "$PAYLOAD_BAD"
OUTCOME="$(field outcome)"
[ "$CODE" = "422" ] && [ "$OUTCOME" = "validation_failed" ]
result $? "invalid 422/validation_failed" "code=$CODE outcome=$OUTCOME"
[ "$(count_rows "$RUN")" = "$BEFORE_ROWS" ]
result $? "invalid created no row"

# --- 5. cleanup ----------------------------------------------------------
if [ "$KEEP" = "1" ]; then
  echo "cleanup: skipped (--keep) — rows kept with name prefix \"$RUN\""
else
  IDS="$(curl -s --max-time 10 "$API_BASE/leads?source=$SOURCE&limit=100" 2>/dev/null |
    python3 -c 'import json,sys
try:
    items = json.load(sys.stdin).get("items", [])
    for i in items:
        if str(i.get("name") or "").startswith(sys.argv[1]):
            print(i.get("lead_id") or i.get("id") or "")
except Exception:
    pass' "$RUN" 2>/dev/null)"
  DELETED=0
  for id in $IDS; do
    [ -n "$id" ] || continue
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X DELETE "$API_BASE/leads/$id")"
    [ "$code" = "204" ] && DELETED=$((DELETED + 1))
  done
  [ "$DELETED" -gt 0 ] && [ "$(count_rows "$RUN")" = "0" ]
  result $? "cleanup removed this run's rows" "deleted=$DELETED"
fi

echo "smoke: $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
