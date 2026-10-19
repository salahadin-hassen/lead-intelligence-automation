#!/bin/sh
set -e

# Render and generic PaaS port mapping
export N8N_PORT="${PORT:-5678}"
export N8N_LISTEN_ADDRESS="0.0.0.0"
export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=320}"

# Parse unified DATABASE_URL or N8N_DATABASE_URL if discrete DB_POSTGRESDB_* vars are not set
if [ -z "$DB_POSTGRESDB_HOST" ] && { [ -n "$N8N_DATABASE_URL" ] || [ -n "$DATABASE_URL" ]; }; then
  TARGET_URL="${N8N_DATABASE_URL:-$DATABASE_URL}"
  case "$TARGET_URL" in
    postgres*://*)
      echo "[n8n-entrypoint] Configuring PostgreSQL from database URL..."
      PARSED_ENV=$(DB_URL="$TARGET_URL" node -e '
        try {
          const u = new URL(process.env.DB_URL);
          const sslParam = u.searchParams.get("sslmode") || u.searchParams.get("ssl");
          const sslEnabled = sslParam !== "disable" && sslParam !== "false";
          console.log(`export DB_TYPE=postgresdb`);
          console.log(`export DB_POSTGRESDB_HOST="${u.hostname}"`);
          console.log(`export DB_POSTGRESDB_PORT="${u.port || 5432}"`);
          console.log(`export DB_POSTGRESDB_USER="${decodeURIComponent(u.username)}"`);
          console.log(`export DB_POSTGRESDB_PASSWORD="${decodeURIComponent(u.password)}"`);
          console.log(`export DB_POSTGRESDB_DATABASE="${u.pathname.replace(/^\//, "")}"`);
          // Neon PostgreSQL requires SSL connections (sslmode=require).
          // n8n requires DB_POSTGRESDB_SSL_ENABLED=true to activate TLS on the pg pool.
          console.log(`export DB_POSTGRESDB_SSL_ENABLED=${sslEnabled ? "true" : "false"}`);
          // Neon certificates are issued by Let'\''s Encrypt (ISRG Root X1), which is trusted
          // natively by Node.js root CAs. Strict TLS certificate verification is enforced.
          console.log(`export DB_POSTGRESDB_SSL_REJECT_UNAUTHORIZED=true`);
        } catch (e) {
          console.error("[n8n-entrypoint] Error parsing DB_URL:", e.message);
        }
      ')
      if [ -n "$PARSED_ENV" ]; then
        eval "$PARSED_ENV"
      fi
      ;;
  esac
fi

# Workflow lifecycle: Import on first boot, preserve bindings on routine restarts.
# Overwriting on restart would reset manual UI credential bindings (e.g. Header Auth).
if [ -f "/workflows/lead-intake.json" ]; then
  if [ "${N8N_FORCE_IMPORT_WORKFLOW:-false}" = "true" ] || ! n8n export:workflow --id=lead-intake --output=/tmp/check-lead-intake.json >/dev/null 2>&1; then
    echo "[n8n-entrypoint] Workflow lead-intake not found in database or force requested; importing from /workflows/lead-intake.json..."
    n8n import:workflow --input=/workflows/lead-intake.json || echo "[n8n-entrypoint] Note: Initial import will complete on database convergence"
    echo "[n8n-entrypoint] Publishing workflow lead-intake..."
    n8n publish:workflow --id=lead-intake || echo "[n8n-entrypoint] Note: Initial publish will complete on database convergence"
  else
    echo "[n8n-entrypoint] Workflow lead-intake already exists; preserving existing configuration and credential bindings."
    # Ensure existing workflow is published and production webhook is registered
    n8n publish:workflow --id=lead-intake || true
  fi
  rm -f /tmp/check-lead-intake.json
fi

echo "[n8n-entrypoint] Starting n8n on port ${N8N_PORT}..."
exec n8n start
