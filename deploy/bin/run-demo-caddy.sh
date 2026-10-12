#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CREDENTIAL_FILE="${DEMO_AUTH_FILE:-$REPO/var/demo-auth.json}"
CADDY_BIN="${CADDY_BIN:-$(command -v caddy || true)}"
PID_FILE="${DEMO_CADDY_PID_FILE:-$REPO/var/run/caddy-demo.pid}"
COMMAND="${1:-run}"

if [ -z "$CADDY_BIN" ] || [ ! -x "$CADDY_BIN" ]; then
  echo "run-demo-caddy: Caddy executable not found; set CADDY_BIN" >&2
  exit 1
fi
if [ ! -f "$CREDENTIAL_FILE" ]; then
  echo "run-demo-caddy: credentials missing; run deploy/bin/create-demo-auth.sh" >&2
  exit 1
fi
if [ "$(stat -c '%a' "$CREDENTIAL_FILE")" != "600" ] ||
   [ "$(stat -c '%u' "$CREDENTIAL_FILE")" != "$(id -u)" ]; then
  echo "run-demo-caddy: credential file must be owned by this user with mode 0600" >&2
  exit 1
fi
if ! command -v jq >/dev/null; then
  echo "run-demo-caddy: jq is required" >&2
  exit 1
fi

auth_user="$(jq -er '.username | select(test("^[A-Za-z0-9._-]+$"))' "$CREDENTIAL_FILE")"
auth_hash="$(jq -er '.password_hash | select(startswith("$argon2id$"))' "$CREDENTIAL_FILE")"
if [ -z "$auth_user" ] || [ -z "$auth_hash" ]; then
  echo "run-demo-caddy: credential file is missing a valid username or Argon2id hash" >&2
  exit 1
fi

export DEMO_AUTH_USER="$auth_user"
export DEMO_AUTH_HASH="$auth_hash"
export LEAD_WWW_ROOT="$REPO/var/www"

case "$COMMAND" in
  validate)
    exec "$CADDY_BIN" validate --config "$REPO/deploy/caddy/Caddyfile.local" --adapter caddyfile
    ;;
  run)
    mkdir -p "$(dirname "$PID_FILE")"
    printf '%s\n' "$$" > "$PID_FILE"
    chmod 600 "$PID_FILE"
    exec "$CADDY_BIN" run --config "$REPO/deploy/caddy/Caddyfile.local" --adapter caddyfile
    ;;
  *)
    echo "usage: $0 [validate|run]" >&2
    exit 2
    ;;
esac
