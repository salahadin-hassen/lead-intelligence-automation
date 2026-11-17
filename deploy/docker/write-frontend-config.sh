#!/bin/sh
# Writes the public frontend configuration (config.js) at container start.
#
#   TURNSTILE_SITE_KEY  PUBLIC Cloudflare Turnstile site key (Render env var).
#                       Never put TURNSTILE_SECRET_KEY here: config.js is
#                       served to every visitor.
#   LEAD_WWW_ROOT       document root (default /var/www)
#
# Generated at start (not build) so the key can be set or rotated in Render
# without rebuilding the image. A missing or malformed key is written as ""
# and the form then refuses to submit (fail closed); intake is never opened.
set -eu

WWW_ROOT="${LEAD_WWW_ROOT:-/var/www}"
SITE_KEY="${TURNSTILE_SITE_KEY:-}"

case "$SITE_KEY" in
  *[!A-Za-z0-9_-]*)
    echo "write-frontend-config: TURNSTILE_SITE_KEY contains unexpected characters; ignoring it (form will refuse to submit)" >&2
    SITE_KEY=""
    ;;
esac
if [ "${#SITE_KEY}" -gt 128 ]; then
  echo "write-frontend-config: TURNSTILE_SITE_KEY is too long; ignoring it (form will refuse to submit)" >&2
  SITE_KEY=""
fi
if [ -z "$SITE_KEY" ]; then
  echo "write-frontend-config: TURNSTILE_SITE_KEY is not set; the lead form will refuse to submit" >&2
fi

cat > "$WWW_ROOT/config.js" <<EOF
/* Generated at container start by write-frontend-config.sh — do not edit. */
window.LEAD_FORM_CONFIG = {
  webhookUrl: "/webhook/lead-intake",
  turnstileSiteKey: "$SITE_KEY",
};
EOF
