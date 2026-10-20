# Cloudflare Pages Deployment Guide

> **Deploying the LeadIntel static frontend and serverless webhook proxy to Cloudflare Pages while preserving the existing Render/PostgreSQL backend.**

---

## 1. Overview & Hybrid Architecture

LeadIntel supports a hybrid deployment model:
- **Cloudflare Pages**: Hosts the high-performance static frontend (`/`, `/demo`, static assets) on Cloudflare's global edge network, and runs a lightweight serverless Pages Function for same-origin webhook proxying.
- **Render & Neon PostgreSQL Backend**: The existing production stack (`leadintel-caddy.onrender.com`, `leadintel-n8n.onrender.com`, `leadintel-api-2y0i.onrender.com`, and Neon PostgreSQL) remains completely unchanged and continues to handle lead validation, deduplication, AI scoring, persistence, and operator notifications.

```text
Visitor Browser
    │
    │  1. HTTPS GET / or /demo
    ▼
Cloudflare Pages Edge (Global CDN)
    ├── GET /               → Serves index.html (LeadIntel Landing Page)
    ├── GET /demo           → Rewritten via _redirects to index.html (Dedicated Demo)
    ├── GET /config.js      → Serves static configuration (webhookUrl: "/webhook/lead-intake")
    │
    │  2. Form Submission: POST /webhook/lead-intake
    ▼
Cloudflare Pages Function (functions/webhook/lead-intake.js)
    │
    │  3. Narrowly scoped serverless proxy (HTTPS POST)
    ▼
Existing Render Edge Ingress (https://leadintel-caddy.onrender.com/webhook/lead-intake)
    │
    │  4. Reverse proxy to n8n workflow
    ▼
Existing Backend Infrastructure (Unchanged)
    ├── n8n Orchestrator (https://leadintel-n8n.onrender.com)
    ├── FastAPI Scoring & Validation (https://leadintel-api-2y0i.onrender.com)
    └── Neon Serverless PostgreSQL (Database: lead_app)
```

---

## 2. Cloudflare Project Configuration

Cloudflare supports deploying this repository through either the dedicated **Cloudflare Pages** workflow or the unified **Cloudflare Workers with Assets** workflow.

### Option A: Dedicated Cloudflare Pages (Recommended)

1. In the [Cloudflare Dashboard](https://dash.cloudflare.com/), navigate to **Workers & Pages** $\rightarrow$ **Create Application**.
2. Select the **Pages** tab (ensure the Pages tab is highlighted, not Workers) $\rightarrow$ **Connect to Git**.
3. Select repository: `salahadin-hassen/lead-intelligence-automation`.
4. Configure build settings:
   - **Framework Preset**: `None` (or `Other`)
   - **Build Command**: `bash deploy/bin/build-pages.sh`
   - **Build Output Directory**: `var/pages-public`
   - **Root Directory**: `/`
5. Click **Save and Deploy**. (Cloudflare Pages deploys static assets natively without requiring `wrangler deploy`).

---

### Option B: Cloudflare Workers CI / Unified Builds (`wrangler.json`)

If your project was connected under the **Workers** tab (where Cloudflare automatically configures the deploy step as `npx wrangler deploy`), the repository provides a root [`wrangler.json`](../wrangler.json) to handle deployment seamlessly:

```json
{
  "$schema": "node_modules/wrangler/config-schema.json",
  "name": "lead-intelligence-automation",
  "main": "functions/webhook/lead-intake.js",
  "compatibility_date": "2024-09-23",
  "assets": {
    "directory": "./var/pages-public",
    "binding": "ASSETS",
    "not_found_handling": "single-page-application",
    "run_worker_first": false
  }
}
```

- **Build Command**: `bash deploy/bin/build-pages.sh`
- **Deploy Command**: `npx wrangler deploy`
- `wrangler.json` directs Wrangler to `./var/pages-public` for static assets and [`functions/webhook/lead-intake.js`](../functions/webhook/lead-intake.js) as the Worker entry point for `/webhook/lead-intake`.

---

## 3. Routing & Path Mechanics

### Static Route Assets (`demo.html` & `demo/index.html`)
LeadIntel's client-side router manages navigation between the marketing overview (`/`) and the interactive demo (`/demo`).
- To prevent Cloudflare's clean-URL normalizer from redirecting rewrite targets (such as `/index.html`) back to `/` via `307 Temporary Redirect`, `deploy/bin/build-pages.sh` emits direct static assets:
  - `var/pages-public/index.html` for `/`
  - `var/pages-public/lead-form.html` for `/lead-form`
  - `var/pages-public/demo.html` for clean `/demo` requests
  - `var/pages-public/demo/index.html` for directory `/demo/` requests
- Direct URL entry and browser refreshes on `/demo` or `/demo/` load the demo view immediately with `HTTP 200 OK`.

### Webhook Proxy Function (`/webhook/lead-intake`)
When the browser submits a lead, it sends a same-origin request to `POST /webhook/lead-intake`.
- Cloudflare Pages automatically maps `functions/webhook/lead-intake.js` to this path.
- The function accepts only HTTP POST requests (rejecting other methods with `HTTP 405 Method Not Allowed`).
- It forwards the JSON payload to `https://leadintel-caddy.onrender.com/webhook/lead-intake`.
- Upstream status codes (`201 Created`, `409 Conflict`, `422 Unprocessable`) and response headers are passed back to the browser transparently.
- If the upstream service is unreachable, it returns a sanitized `HTTP 502 Bad Gateway` error matching the frontend's expected error contract.

### Security Headers (`_headers`)
The build script emits standard defensive security headers applied to all static routes:
```text
/*
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Referrer-Policy: no-referrer
```

---

## 4. Post-Deployment Verification

Once Cloudflare completes the initial build, verify the live deployment:

1. **Root Landing Page:**
   - Open `https://<project-name>.pages.dev/`.
   - Verify the landing page loads at the top (`scrollY: 0`).
2. **Demo CTA Navigation:**
   - Click "Try Demo" or any demo button.
   - Verify the URL changes to `https://<project-name>.pages.dev/demo` and the interactive intake card is displayed.
3. **Direct Navigation & Refresh:**
   - Refresh the page while on `/demo` (or enter `https://<project-name>.pages.dev/demo` directly in a new tab).
   - Verify the page reloads the demo view cleanly rather than returning a 404 error.
4. **Configuration Check:**
   - Inspect `https://<project-name>.pages.dev/config.js`.
   - Verify `webhookUrl` is set to `"/webhook/lead-intake"` and contains no localhost development references.
5. **Form Submission:**
   - Fill out an inquiry with synthetic details and click "Submit Lead Inquiry".
   - Verify the submission returns an HTTP 201 Created confirmation with the AI score breakdown.
6. **Duplicate Conflict Check:**
   - Click "Test Duplicate Detection (Replay Lead)".
   - Verify the UI renders the amber "Duplicate Ingestion Blocked (409)" banner.

---

## 5. Expected Failure Modes & Troubleshooting

| Failure Mode | Root Cause | Resolution / Behavior |
| :--- | :--- | :--- |
| **`Could not detect a directory containing static files`** | Cloudflare Workers CI ran `npx wrangler deploy` without an explicit assets directory configuration. | Resolved by the committed [`wrangler.json`](../wrangler.json), which maps `assets.directory` to `./var/pages-public`. |
| **Cold-Start Latency (~40–50s)** | Render free-tier containers sleep after 15 minutes of inactivity. | The client form includes a 120-second connection timeout to accommodate container spin-up. Warm instances respond in ~4–5 seconds. |
| **HTTP 502 Bad Gateway** | Render Caddy or n8n service is temporarily down or unreachable. | The Pages Function returns `{ "outcome": "upstream_error", "apiStatus": 502 }`, which triggers the user-friendly retry banner in the frontend. |
| **HTTP 405 Method Not Allowed** | Direct GET or non-POST request to `/webhook/lead-intake`. | Normal behavior; the Pages Function restricts the proxy to POST only and returns `Allow: POST`. |
| **404 on `/demo` Refresh** | `_redirects` file missing from build output. | Verify that `deploy/bin/build-pages.sh` executed successfully and generated `var/pages-public/_redirects`. |

---

## 6. Rollback & Maintenance

- **Cloudflare Rollback:** If an issue occurs with a newly deployed frontend version, navigate to the Cloudflare Pages dashboard $\rightarrow$ **Deployments** $\rightarrow$ select the previous successful deployment $\rightarrow$ click **Rollback**.
- **Backend Independence:** Cloudflare Pages is decoupled from the Render backend. The existing Render URL (`https://leadintel-caddy.onrender.com`) remains independently operational at all times.

---

## 7. Next Steps After Deployment

- Cloudflare automatically issues a unique `https://<project-name>.pages.dev` URL upon initial deployment. *(Do not assume or invent this URL prior to deployment).*
- Once the deployment is verified, update the repository's [`README.md`](../README.md) to list the official Cloudflare Pages URL alongside the Render production endpoint.
