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

# Gmail workflow (gmail-lead-intake): imported INACTIVE, and only when missing.
# N8N_FORCE_IMPORT_WORKFLOW above never touches it. To ship a changed
# n8n/gmail-lead-intake.json, set N8N_FORCE_IMPORT_GMAIL_WORKFLOW=true for one
# deploy (docs/gmail-lead-ingestion.md, Section 7). The forced import:
#   - imports only gmail-lead-intake, never lead-intake;
#   - keeps the stored workflow's credential bindings and label selection;
#   - is skipped once the stored workflow came from this exact file, so a flag
#     left set does not re-import on later restarts;
#   - is refused while the Gmail workflow is active.
# Failures are logged as ERROR and leave the stored workflow unchanged; n8n
# still starts so website intake keeps running.
GMAIL_WF=/workflows/gmail-lead-intake.json
GMAIL_HELPER=/workflows/gmail-workflow-import.js
if [ -f "$GMAIL_WF" ]; then
  GMAIL_TMP=$(mktemp -d)
  # Only "No workflows found" means missing; any other export failure (e.g. the
  # database is unreachable) must not be mistaken for it and trigger an import.
  if n8n export:workflow --id=gmail-lead-intake --output="$GMAIL_TMP/stored.json" >"$GMAIL_TMP/export.log" 2>&1; then
    GMAIL_STATE=present
  elif grep -q "No workflows found" "$GMAIL_TMP/export.log"; then
    GMAIL_STATE=missing
  else
    GMAIL_STATE=unknown
  fi

  GMAIL_IMPORT=false
  case "$GMAIL_STATE" in
    missing)
      echo "[n8n-entrypoint] Workflow gmail-lead-intake not found; importing it inactive from $GMAIL_WF..."
      if node "$GMAIL_HELPER" prepare "$GMAIL_WF" "$GMAIL_TMP/import.json"; then
        GMAIL_IMPORT=true
      fi
      ;;
    present)
      if [ "${N8N_FORCE_IMPORT_GMAIL_WORKFLOW:-false}" = "true" ]; then
        GMAIL_RC=0
        node "$GMAIL_HELPER" prepare "$GMAIL_WF" "$GMAIL_TMP/import.json" "$GMAIL_TMP/stored.json" || GMAIL_RC=$?
        case "$GMAIL_RC" in
          0)
            echo "[n8n-entrypoint] N8N_FORCE_IMPORT_GMAIL_WORKFLOW=true: updating gmail-lead-intake only (credential bindings and label kept)..."
            GMAIL_IMPORT=true
            ;;
          3)
            echo "[n8n-entrypoint] gmail-lead-intake already matches $GMAIL_WF; nothing imported. Remove N8N_FORCE_IMPORT_GMAIL_WORKFLOW."
            ;;
          4)
            echo "[n8n-entrypoint] ERROR: forced Gmail import refused because gmail-lead-intake is active; stored workflow left unchanged."
            ;;
          *)
            echo "[n8n-entrypoint] ERROR: could not prepare the forced Gmail import; stored workflow left unchanged."
            ;;
        esac
      else
        echo "[n8n-entrypoint] Workflow gmail-lead-intake already exists; preserving existing configuration and credential bindings."
      fi
      ;;
    *)
      echo "[n8n-entrypoint] ERROR: could not read gmail-lead-intake from the n8n database; Gmail provisioning skipped."
      ;;
  esac

  if [ "$GMAIL_IMPORT" = "true" ]; then
    if n8n import:workflow --input="$GMAIL_TMP/import.json" &&
      n8n export:workflow --id=gmail-lead-intake --output="$GMAIL_TMP/after.json" >/dev/null 2>&1 &&
      node "$GMAIL_HELPER" verify "$GMAIL_WF" "$GMAIL_TMP/after.json"; then
      echo "[n8n-entrypoint] gmail-lead-intake imported inactive; bind its credentials in the n8n UI before activating."
    else
      echo "[n8n-entrypoint] ERROR: gmail-lead-intake import failed or could not be verified; check it in the n8n UI before use."
    fi
  fi
  rm -rf "$GMAIL_TMP"
fi

echo "[n8n-entrypoint] Starting n8n on port ${N8N_PORT}..."
exec n8n start
