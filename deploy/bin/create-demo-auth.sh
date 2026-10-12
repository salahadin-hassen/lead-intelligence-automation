#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${DEMO_AUTH_FILE:-$REPO/var/demo-auth.json}"
CADDY_BIN="${CADDY_BIN:-$(command -v caddy || true)}"

if [ -z "$CADDY_BIN" ] || [ ! -x "$CADDY_BIN" ]; then
  echo "create-demo-auth: Caddy executable not found; set CADDY_BIN" >&2
  exit 1
fi
if [ -e "$DEST" ]; then
  echo "create-demo-auth: refusing to overwrite existing credentials: $DEST" >&2
  exit 1
fi
if ! command -v openssl >/dev/null || ! command -v jq >/dev/null; then
  echo "create-demo-auth: openssl and jq are required" >&2
  exit 1
fi

umask 077
mkdir -p "$(dirname "$DEST")"
password="$(openssl rand -base64 36 | tr -d '\n')"
password_hash="$(printf '%s\n' "$password" | "$CADDY_BIN" hash-password --algorithm argon2id)"
temporary="${DEST}.tmp.$$"
trap 'rm -f "$temporary"' EXIT

printf '{"username":"demo-reviewer","password":"%s","password_hash":"%s"}\n' \
  "$password" "$password_hash" | jq -e -c . > "$temporary"
chmod 600 "$temporary"
mv "$temporary" "$DEST"
trap - EXIT

echo "create-demo-auth: credentials created with mode 0600 at $DEST"
