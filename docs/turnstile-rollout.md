# Turnstile Rollout Runbook

How to switch production to server-side Cloudflare Turnstile verification on `POST /leads` without a period in which legitimate website leads are refused. The repository changes are **not** a deployment: nothing below has been applied to production yet.

## Verified production state (owner exports and Render, 2026-11-16/17)

| Component | Production today | Repository (local working tree) |
| :--- | :--- | :--- |
| `leadintel-api` | Commit `9109f81`. `AI_INSIGHTS_ENABLED=true`, `OPENAI_API_KEY` present, budget 20/h and 100/day. No Turnstile gate. | Adds the Turnstile gate on `POST /leads` (Task 05A) |
| `leadintel-caddy` | Page from `9109f81` (AI panel, no widget). Proxies all `/webhook/*`. | Adds the Turnstile widget and Task 04 `source` values; `config.js` written at start from `TURNSTILE_SITE_KEY`; proxies only `/webhook/lead-intake` |
| n8n `lead-intake`: "Lead Intake & Qualification", ID `lead-intake`, **active**, 27 nodes | Repository version `2fc400f` (pre-AI graph) plus one live-only edit: `DEFAULT_API_BASE_URL = 'https://leadintel-api-2y0i.onrender.com'`. Routes 201/409/422; everything else goes to `Respond Upstream Error` (502). FastAPI credential is named **"Header Auth account"**. | `n8n/lead-intake.json` is the 32-node AI-insights graph plus Turnstile (34 nodes), with a `localhost` default URL and a placeholder credential reference. **Never import it over the live workflow.** |
| n8n `gmail-lead-intake`, **inactive**, 16 nodes | Same as the repository before Task 05B: no `X-Intake-Channel`; 401 and 403 go to `auth_error` | Adds the fixed `X-Intake-Channel: gmail` header |
| n8n `lead-intake-ai-demo` (ID `tWb111ZniDQAk5Hy`), **inactive**, 9 nodes | Repository version `97656c7`, **without** the `Attach AI Insights` lead-ID safeguard | 10-node version with the safeguard |

The only active intake path is website → `lead-intake` → `POST /leads`.

**AI insights on the website:** the live `lead-intake` sends no request for insights and returns no `ai_insights` field, so the demo page's AI panel shows **"Not returned"**. This release does not change that. An API key being configured does **not** mean AI insights are connected to the website.

## Scope of this release

**In scope:**
- The public Turnstile widget (`leadintel-caddy`).
- Narrowed Caddy routing (`leadintel-caddy`).
- Three in-place edits plus two new nodes in the active `lead-intake` workflow.
- Turnstile enforcement in FastAPI (`leadintel-api`), deployed last.

**Out of scope:**
- Activating `gmail-lead-intake` or `lead-intake-ai-demo`: both stay inactive.
- Any AI-insights wiring.
- Any n8n image change: nothing in the n8n image is needed for this release.

**Why this order is safe:** every change except the API is backward-compatible with the current API `9109f81`. The old workflow drops the extra `turnstile_token` body field, and the old API ignores the `X-Turnstile-Token` header and never returns 403/503 for leads. The API therefore goes **last**. From then on, production always enforces verification (`APP_ENV=production`, and Render sets `RENDER=true`). `TURNSTILE_ENABLED=false` is ignored there, and a missing secret refuses website leads with HTTP 503.

## Hazards specific to this deployment

- **Never set `N8N_FORCE_IMPORT_WORKFLOW=true`.** It would replace the live `lead-intake` with the repository file, which has the AI graph, a `localhost` API default and a credential reference that doesn't exist in production. Edit the live workflow in place instead (Step 7).
- **n8n re-publishes `lead-intake` on every start.** The entrypoint runs `n8n publish:workflow --id=lead-intake` on each boot, so treat any *saved* edit as something that goes live at the next n8n restart, whether or not you pressed Publish. Never leave a half-finished saved edit behind.
- **A push redeploys every service that has Auto-Deploy on.** The API must not deploy early, and an n8n redeploy brings no benefit here but costs a cold start of several minutes.
- **Saving environment variables can restart a service.** Set them while the old code is still running, which ignores them.

## Before you start

1. **Approvals and timing:** pick a low-traffic window. Have the owner's Render, n8n and Cloudflare dashboard access ready.
2. **Backups:**
   - In n8n, open each of the three workflows and download its JSON (⋯ → Download). Keep these files private; they contain credential references.
   - In Render, note the current deploy (commit and time) of `leadintel-api`, `leadintel-caddy` and `leadintel-n8n`. These are the rollback targets.
3. **Blueprint sync:** check whether Render manages these services through a Blueprint with sync enabled. If so, pushing `render.yaml` applies `TURNSTILE_ALLOWED_HOSTNAMES` and `TURNSTILE_EXPECTED_ACTION` to the API. That's harmless on the old code, but confirm the values afterwards.

## Ordered steps

1. **Create the Turnstile widget** in Cloudflare (Turnstile → Add widget). Hostname: `leadintel.sahelyon.com` only. Mode: Managed.
2. **Copy the site key and secret key** from that page. The secret goes only into Render's API environment. Never paste either key into the repository, a ticket or a chat.
3. **Set the API environment** on `leadintel-api`:
   - `TURNSTILE_SECRET_KEY` = the secret
   - `TURNSTILE_ALLOWED_HOSTNAMES` = `leadintel.sahelyon.com`
   - `TURNSTILE_EXPECTED_ACTION` = `lead-intake`
   Any restart this triggers runs `9109f81`, which ignores these variables.
4. **Set the public site key** on `leadintel-caddy`: `TURNSTILE_SITE_KEY` = the site key. It must be set **before** the new Caddy image starts; without it, the new page refuses every submission.
5. **Turn Auto-Deploy off** on `leadintel-api` (required) and on `leadintel-n8n` (recommended: avoids a pointless n8n restart and re-publish). Leave it on for `leadintel-caddy`, or deploy Caddy manually in Step 6.
6. **Push and deploy the frontend.** When `leadintel-caddy` is live:
   - Open `https://leadintel.sahelyon.com/demo` and confirm the widget renders and shows "Verification complete."
   - Fetch `/config.js` and confirm it contains only `webhookUrl` and the public `turnstileSiteKey`.
   - **Do not submit.**
   From now on the page refuses to submit without a token, so a broken widget blocks intake: see Rollback A.
7. **Edit the live `lead-intake` workflow in place.** Open the existing workflow "Lead Intake & Qualification"; the URL must end in `/workflow/lead-intake`. Do **not** import a file and do **not** duplicate it. Make all of the following edits in one sitting before saving:

   **7a. `Prepare Lead Payload`** (Code node). Keep everything above the final `return`, including the live `DEFAULT_API_BASE_URL`. Replace only the final `return [{ … }];` block with:

   ```javascript
   // Cloudflare Turnstile token for FastAPI's X-Turnstile-Token header. It is
   // kept OUT of `lead`, so it is never part of the lead body or stored; FastAPI
   // verifies it once with Cloudflare (tokens are single-use). Anything that is
   // not a non-empty string of at most 2048 characters becomes '' and FastAPI
   // rejects the submission (fail closed). Request headers are never read here.
   const rawTurnstileToken = body.turnstile_token;
   const turnstileToken =
     typeof rawTurnstileToken === 'string' && rawTurnstileToken.length > 0 && rawTurnstileToken.length <= 2048
       ? rawTurnstileToken
       : '';

   return [{
     json: {
       lead,
       turnstileToken,
       apiLeadsUrl: `${apiBaseUrl}/leads`,
       apiBaseUrlSource,
       qualifiedThreshold,
       thresholdSource,
     },
   }];
   ```
   The six-field `lead` object above it stays exactly as it is.

   **7b. `Create Lead in FastAPI`** (HTTP Request). Under *Send Headers*, keep `Content-Type: application/json` and **add** one header:
   - Name: `X-Turnstile-Token`
   - Value, in expression mode: `{{ $json.turnstileToken }}`

   Change nothing else. Authentication stays *Generic Credential → Header Auth* with **"Header Auth account"**, the body stays `{{ JSON.stringify($json.lead) }}`, timeout stays 10000 ms, and *Full Response* and *Never Error* stay on.

   **7c. Add the two response nodes.** Copy the JSON below and paste it onto the canvas (Ctrl/Cmd+V). The nodes have no credentials.

   ```json
   {
     "nodes": [
       {
         "parameters": {
           "respondWith": "json",
           "responseBody": "={{ JSON.stringify({ outcome: 'verification_failed', apiStatus: 403, detail: 'Verification failed. Complete the verification and try again.', message: 'Verification failed. Complete the verification and try again.' }) }}",
           "options": { "responseCode": 403 }
         },
         "name": "Respond Verification Failed",
         "type": "n8n-nodes-base.respondToWebhook",
         "typeVersion": 1.5,
         "position": [960, 400]
       },
       {
         "parameters": {
           "respondWith": "json",
           "responseBody": "={{ JSON.stringify({ outcome: 'verification_unavailable', apiStatus: 503, detail: 'Verification service is temporarily unavailable. Please try again shortly.', message: 'Verification service is temporarily unavailable. Please try again shortly.' }) }}",
           "options": { "responseCode": 503 }
         },
         "name": "Respond Verification Unavailable",
         "type": "n8n-nodes-base.respondToWebhook",
         "typeVersion": 1.5,
         "position": [960, 560]
       }
     ],
     "connections": {}
   }
   ```

   **7d. `Route on API Status`** (Switch, Rules mode). n8n stores connections by output **position**, and adding rules moves the fallback output. So:
   1. **First delete** the connection from the switch's *Fallback* output to `Respond Upstream Error`.
   2. Add a routing rule: `{{ $json.statusCode }}`, *Number → is equal to*, `403`, *Rename Output* on, output name `verification_failed`.
   3. Add a second rule the same way with `503` and output name `verification_unavailable`.
   4. Keep the three existing rules (201 `created`, 409 `duplicate`, 422 `validation_failed`) unchanged and first, and keep *Options → Fallback Output: Extra Output*.
   5. Connect the outputs: `verification_failed` → `Respond Verification Failed`, `verification_unavailable` → `Respond Verification Unavailable`, *Fallback* → `Respond Upstream Error`.

   **7e. Check before saving** (hover or open each output):

   | Output | Must connect to |
   | :--- | :--- |
   | `created` | `Respond Created` **and** `Extract Lead + Score` (unchanged) |
   | `duplicate` | `Respond Duplicate` |
   | `validation_failed` | `Respond Validation Failed` |
   | `verification_failed` | `Respond Verification Failed` only |
   | `verification_unavailable` | `Respond Verification Unavailable` only |
   | Fallback | `Respond Upstream Error` |

   The two new nodes must have **no** outgoing connections. Don't touch any node after `Extract Lead + Score` (qualification, audit, Telegram, WhatsApp).

   **7f. Save, then publish** if your editor has a separate Publish action, so the live version is the edited one. Confirm the workflow still shows as active/published.

   **7g. Confirm identity:**
   - The URL is still `/workflow/lead-intake`.
   - `Lead Webhook` still shows path `lead-intake`, method POST, and its production URL ends in `/webhook/lead-intake`.
   - `Create Lead in FastAPI` still uses "Header Auth account".
   - The Telegram nodes use "Telegram Lead Alerts" and the WhatsApp nodes "WhatsApp Lead Alerts".
   - There are no new workflows in the list (no "copy").
   - Optional: download the edited workflow's JSON and compare it offline with the pre-edit backup. Only `Prepare Lead Payload`, `Create Lead in FastAPI` and `Route on API Status` should differ, plus the two new nodes. The ID, webhook ID and every credential binding must be identical.
8. **Leave `gmail-lead-intake` and `lead-intake-ai-demo` inactive.** Don't edit or activate them in this release.
9. **Pre-API check, without submitting:**
   - The widget renders on `/demo`.
   - `lead-intake` is active, with the edits from Step 7.
   - Both other workflows are inactive.
   - No `N8N_FORCE_IMPORT_WORKFLOW` is set on `leadintel-n8n`.
10. **Deploy the API last:** on `leadintel-api`, Manual Deploy → the pushed commit. Enforcement starts when the deploy is live.
11. **Confirm the API:**
    - `GET /health` returns `{"status":"ok"}`.
    - `GET /openapi.json` lists the `X-Turnstile-Token` header on `POST /leads`, which proves the new code is live.
    - The Render environment shows `TURNSTILE_SECRET_KEY` present (value masked) and `APP_ENV=production`.
    - The logs show no `Turnstile verification unavailable (not_configured)`.
    Then restore the Auto-Deploy settings from Step 5 to their previous values.
12. **End-to-end test only under an approved synthetic test procedure.** It must define the synthetic data, expected Telegram/WhatsApp behaviour and how the test lead is identified afterwards (see the client demo guide). Without one, don't submit to production. Expect the AI panel to show "Not returned".

## Rollback

Roll back in this order: **API first**, then Caddy, then n8n. The API is the only change that refuses old callers.

- **A. Widget problem before Step 10** ("Verification could not load / is not configured"; intake blocked by the page):
  - Fix `TURNSTILE_SITE_KEY` on `leadintel-caddy` (restart only).
  - Or roll `leadintel-caddy` back to the deploy noted in "Before you start".
- **B. Website leads refused after Step 10** (403/503 for real visitors):
  - Roll `leadintel-api` back to its `9109f81` deploy (Render → Deploys → that deploy → Rollback/Redeploy). This alone restores intake: the edited workflow and the new page both work with the old API.
  - Do **not** try `TURNSTILE_ENABLED=false`; production ignores it by design.
- **C. Workflow problem** (for example an error in `Prepare Lead Payload`):
  - With the API still on `9109f81`: in the editor, revert the three nodes to their backup values (paste the original `jsCode` from the backup JSON, remove the extra header, remove the two rules and nodes, reconnect *Fallback* → `Respond Upstream Error`). Or restore the pre-edit version from *Workflow history* if your n8n offers it. Save and publish.
  - If the API is already enforcing, do **B first**. An unedited workflow sends no token, so the new API would refuse every lead.
  - Never restore by importing a file as a new workflow, and never with `N8N_FORCE_IMPORT_WORKFLOW`.
- **D. Caddy rollback after Step 10:** do **B first**. The old page sends no token, so it can't work with the new API.
- **E. Secret leaked or rotated:** rotate it in Cloudflare, update `TURNSTILE_SECRET_KEY` on Render, and let the API restart. Website submissions fail closed during the restart.

## Inactive workflows: requirements before any future activation

- **`gmail-lead-intake`:** before it's ever activated against the Turnstile-enforcing API, `Create Lead in FastAPI` must send the literal header `X-Intake-Channel: gmail` (never an expression built from email or webhook data). It must keep its bearer credential ("Header Auth account"). Without the header, every Gmail lead is refused with 403, which the workflow logs as `auth_error`. The repository's `n8n/gmail-lead-intake.json` already contains the header. Use the documented `N8N_FORCE_IMPORT_GMAIL_WORKFLOW` path or an equivalent in-place edit (`docs/gmail-lead-ingestion.md`), only while the workflow is inactive.
- **`lead-intake-ai-demo`:** stays inactive and unsupported. Its live copy lacks the lead-ID safeguard. It forwards no Turnstile token, so once the API enforces verification every call would be refused (403 → its upstream-error response). It isn't part of the n8n image, so no redeploy can import or activate it. Caddy no longer forwards its path, but `leadintel-n8n.onrender.com` would still accept its webhook if someone activated it.

## What this release does not cover

- **Direct n8n access:** `leadintel-n8n.onrender.com` and `leadintel-caddy.onrender.com` remain publicly reachable. The FastAPI gate is what protects lead creation on every path: unverified requests are refused before any lead, score, notification or AI call.
- **Execution exhaustion:** each junk request still starts an n8n execution and one API call before the 403. There is still no rate limit.
- **The smoke script** (`deploy/bin/smoke.sh`) posts leads without a token, so it only works against a local stack with verification disabled (non-production API, `TURNSTILE_ENABLED=false`).
