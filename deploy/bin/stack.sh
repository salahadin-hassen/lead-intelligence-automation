#!/usr/bin/env bash
# Reproducible start/stop/status/logs for the whole stack WITHOUT systemd
# or root. On a server, systemd (deploy/systemd/*.service) does this job at
# boot using the SAME env files and run scripts; this script is the
# manual/local path and the basis of the deployment validation.
#
#   deploy/bin/stack.sh start     render form (if form.env), start api + n8n,
#                                 wait for both, then healthcheck (fails loudly)
#   deploy/bin/stack.sh stop      stop api + n8n (TERM, then KILL)
#   deploy/bin/stack.sh restart   stop + start
#   deploy/bin/stack.sh status    pid + full health summary
#   deploy/bin/stack.sh logs      follow both logs
#   deploy/bin/stack.sh render    only re-render the form document root
#
# Env files (copy from the adjacent *.env.example and edit):
#   deploy/env/api.env    deploy/env/n8n.env    deploy/env/form.env
# Override their locations with API_ENV_FILE / N8N_ENV_FILE / FORM_ENV_FILE.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN="$REPO/var/run"
LOG="$REPO/var/log"
API_ENV="${API_ENV_FILE:-$REPO/deploy/env/api.env}"
N8N_ENV="${N8N_ENV_FILE:-$REPO/deploy/env/n8n.env}"
FORM_ENV="${FORM_ENV_FILE:-$REPO/deploy/env/form.env}"

die() { echo "stack: $*" >&2; exit 1; }

load_env() {
  [ -f "$1" ] || die "missing env file: $1 (cp $1.example $1 and edit values)"
  set -a
  # shellcheck disable=SC1090
  . "$1"
  set +a
}

pid_of() {
  if [ -f "$RUN/$1.pid" ]; then cat "$RUN/$1.pid"; fi
}

alive() { # alive <name> <cmdline-token>...  (any token counts as "ours")
  local name p token cmd
  name="$1"
  shift
  p="$(pid_of "$name")"
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null || return 1
  # Guard against pid reuse: require the recorded process to still be the
  # kind of process we started (Linux /proc; documented limitation).
  [ -r "/proc/$p/cmdline" ] || return 0
  cmd="$(tr '\0' ' ' < "/proc/$p/cmdline")"
  for token in "$@"; do
    case "$cmd" in
      *"$token"*) return 0 ;;
    esac
  done
  return 1
}

tokens_for() { # tokens_for <name> -> cmdline tokens that identify our process
  case "$1" in
    api) printf '%s\n' "run-api.sh" "uvicorn" ;;
    n8n) printf '%s\n' "run-n8n.sh" "n8n" ;;
  esac
}

start_one() { # start_one <name> <log> <cmd> [args...]
  local name="$1" log="$2"
  shift 2
  # shellcheck disable=SC2046
  if alive "$name" $(tokens_for "$name"); then
    echo "stack: $name already running (pid $(pid_of "$name"))"
    return 0
  fi
  mkdir -p "$RUN" "$LOG"
  nohup "$@" >>"$log" 2>&1 &
  echo $! > "$RUN/$name.pid"
  echo "stack: $name started (pid $!, log ${log#"$REPO/"})"
}

stop_one() { # stop_one <name>
  local name="$1" p i
  # shellcheck disable=SC2046
  if ! alive "$name" $(tokens_for "$name"); then
    echo "stack: $name not running"
    rm -f "$RUN/$name.pid"
    return 0
  fi
  p="$(pid_of "$name")"
  kill "$p" 2>/dev/null
  for i in $(seq 1 15); do
    kill -0 "$p" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$p" 2>/dev/null; then
    echo "stack: $name did not stop in 15s — sending KILL"
    kill -9 "$p" 2>/dev/null
    sleep 1
  fi
  rm -f "$RUN/$name.pid"
  echo "stack: $name stopped"
}

wait_for() { # wait_for <url> <tries>
  local url="$1" tries="${2:-30}" i=0
  while [ "$i" -lt "$tries" ]; do
    if curl -s -o /dev/null --max-time 2 "$url" 2>/dev/null; then
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

tail_hint() { # tail_hint <log>
  echo "--- last 30 lines of ${1#"$REPO/"}) ---" >&2
  tail -n 30 "$1" >&2 || true
}

# n8n applies workflow publications asynchronously (publication outbox
# processed by the RUNNING instance; a freshly started instance first serves
# its last applied version and reconciles a few seconds later). Until the
# applied pointer (workflow_published_version) equals the desired one
# (workflow_entity.activeVersionId), executions run STALE workflow content.
# Wait for that convergence so "stack up" means "the imported workflow runs".
wait_version_sync() { # wait_version_sync <sqlite-file> <tries>
  local db="$1" tries="${2:-60}" i=0 state
  [ -f "$db" ] || return 0
  while [ "$i" -lt "$tries" ]; do
    state="$(python3 - "$db" <<'PY' 2>/dev/null || echo pending
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1], timeout=2)
    queued = c.execute(
        "select count(*) from workflow_publication_outbox where status not in ('completed')"
    ).fetchone()[0]
    rows = c.execute(
        "select w.id, p.publishedVersionId, w.activeVersionId"
        " from workflow_entity w"
        " left join workflow_published_version p on p.workflowId = w.id"
    ).fetchall()
    stale = [r for r in rows if r[2] is not None and r[1] != r[2]]
    print("ok" if queued == 0 and not stale else "pending")
except Exception:
    print("pending")
PY
)"
    [ "$state" = "ok" ] && return 0
    sleep 2
    i=$((i + 1))
  done
  return 1
}

cmd_start() {
  load_env "$API_ENV"
  load_env "$N8N_ENV"
  mkdir -p "$RUN" "$LOG"

  if [ -f "$FORM_ENV" ]; then
    FORM_ENV_FILE="$FORM_ENV" "$REPO/deploy/bin/render-form.sh" || die "form render failed"
  else
    echo "stack: $FORM_ENV not present — skipping form render (cp deploy/env/form.env.example to use it)"
  fi

  start_one api "$LOG/api.log" "$REPO/deploy/bin/run-api.sh"
  start_one n8n "$LOG/n8n.log" "$REPO/deploy/bin/run-n8n.sh"

  local api_url="http://127.0.0.1:${API_PORT:-8000}"
  local n8n_url="http://127.0.0.1:${N8N_PORT:-5678}"

  if ! wait_for "$api_url/health" 30; then
    tail_hint "$LOG/api.log"
    die "api did not answer /health within 30s"
  fi
  # n8n needs a while on a fresh data folder (schema bootstrap).
  if ! wait_for "$n8n_url/healthz" 90; then
    tail_hint "$LOG/n8n.log"
    die "n8n did not answer /healthz within 90s"
  fi
  # Workflow publications (import/publish) are applied asynchronously; wait
  # until the applied version matches the desired one (see wait_version_sync).
  if ! wait_version_sync "${N8N_USER_FOLDER:-$REPO/var/n8n}/.n8n/database.sqlite" 60; then
    tail_hint "$LOG/n8n.log"
    die "n8n workflow publication did not converge within 120s (applied version != active version)"
  fi
  sleep 3 # webhook registration lags /healthz slightly on cold start
  if ! "$REPO/deploy/bin/healthcheck.sh"; then
    tail_hint "$LOG/n8n.log"
    die "healthcheck failed after start"
  fi
}

cmd_stop() {
  stop_one n8n
  stop_one api
}

cmd_status() {
  if alive api uvicorn; then echo "api: running (pid $(pid_of api))"; else echo "api: stopped"; fi
  if alive n8n n8n; then echo "n8n: running (pid $(pid_of n8n))"; else echo "n8n: stopped"; fi
  "$REPO/deploy/bin/healthcheck.sh"
}

case "${1:-}" in
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) cmd_status ;;
  logs)   mkdir -p "$LOG"; exec tail -n 50 -f "$LOG/api.log" "$LOG/n8n.log" ;;
  render) FORM_ENV_FILE="$FORM_ENV" "$REPO/deploy/bin/render-form.sh" ;;
  *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
