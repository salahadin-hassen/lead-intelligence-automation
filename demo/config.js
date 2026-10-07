/* =====================================================================
 * Lead form configuration — THE single place the n8n webhook URL lives.
 * =====================================================================
 * Local development default is committed below. A deployment replaces
 * (never edits by hand) this file: deploy/bin/render-form.sh renders it
 * from form.env's DEMO_N8N_WEBHOOK_URL into the served document root,
 * e.g.  DEMO_N8N_WEBHOOK_URL=https://<domain>/webhook/lead-intake
 * (same-origin behind the reverse proxy) or an absolute n8n URL.
 * ===================================================================== */
window.LEAD_FORM_CONFIG = {
  webhookUrl: "http://localhost:5679/webhook/lead-intake",
};
