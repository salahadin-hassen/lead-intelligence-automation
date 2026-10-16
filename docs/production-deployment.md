# Production Deployment Guide: Zero-Cost, Credit-Card-Free Architecture

This guide details the complete production deployment of LeadIntel to public cloud infrastructure with **$0 budget** and **no credit card required**.

---

## 1. Production Architecture & Free-Tier Networking

```text
Internet (Recruiter / Client Browser)
       │  HTTPS (Render Edge TLS)
       ▼
[leadintel-caddy] (Port 10000, Caddy 2.8 Alpine)
   ├── GET /                   → Static LeadIntel V2 Website
   └── POST /webhook/*         → Reverse Proxy over Public HTTPS
                                       │
                                       ▼
                             [leadintel-n8n] (Port 5678, n8n 2.41.7)
                                │  • Same-origin intake at /webhook/lead-intake
                                │  • Dispatches POST https://<leadintel-api>.onrender.com/leads
                                │  • Authenticates via native n8n Header Auth credential
                                │  • Evaluates qualification threshold (score >= 70)
                                │  • Dispatches Telegram/WhatsApp alerts
                                │
               ┌────────────────┴────────────────┐
               ▼                                 ▼
       [leadintel-api]                   [Neon PostgreSQL: n8n]
     (Port 8000, Python 3.12)           (Workflow state & execution history)
        │  • Validates payload (Pydantic)
        │  • Fail-closed production auth
        │  • Prevents duplicates (409 Conflict)
        ▼
   [Neon PostgreSQL: lead_app]
     (Persistent leads & scores table)
```

### Critical Free-Tier Rules & Security Guarantees
1. **Public HTTPS Routing**:
   Render Free Web Services **cannot receive private-network traffic**. Internal hostnames like `leadintel-api:8000` fail on the free tier. All inter-service calls flow over **public HTTPS endpoints** (`https://<service-slug>.onrender.com`).
2. **Fail-Closed Production Security**:
   FastAPI operates with `APP_ENV=production`. If `API_AUTH_TOKEN` is missing, the application fails loudly at startup lifespan and protected `/leads*` endpoints fail-closed. `/health` remains public for Render health probes.
3. **Explicit TLS & Strict Certificate Verification**:
   Both n8n and FastAPI connect to Neon PostgreSQL with encrypted TLS connections. For n8n, the container entrypoint explicitly activates TLS on the connection pool (`DB_POSTGRESDB_SSL_ENABLED=true`) and enforces strict certificate validation (`DB_POSTGRESDB_SSL_REJECT_UNAUTHORIZED=true`), preserving the intent of Neon's `sslmode=require`. Neon certificates are signed by ISRG Root X1 (Let's Encrypt), which is trusted natively by Node.js and system CA bundles. Certificate verification is never bypassed.
4. **Memory Footprint**:
   Render Free Web Services allocate 512 MB RAM per instance. n8n 2.41.7 baseline memory consumption is ~180-260 MB, leaving ~250 MB headroom.

---

## 2. Infrastructure & Cost Budget

| Component | Provider | Free Plan Details | Credit Card Required? |
| :--- | :--- | :--- | :--- |
| **Edge Proxy & Website** | Render Web Service | 750 free instance hours / month | **No** |
| **Application API** | Render Web Service | 512 MB RAM, spins down on idle | **No** |
| **Workflow Engine** | Render Web Service | 512 MB RAM, spins down on idle | **No** |
| **PostgreSQL Database** | Neon Serverless | 0.5 GiB storage, permanent free tier | **No** |
| **Total Cost** | | **$0.00 / month forever** | |

> [!NOTE]
> **Free Tier Cold Starts**: Render free instances automatically spin down after 15 minutes of inactivity. When a visitor first loads the page or submits a lead after idle time, the initial request experiences a ~50-second wake-up delay while containers boot. Subsequent requests respond instantly.

---

## 3. Step 1: Provision Free Serverless PostgreSQL on Neon

Neon provides free cloud PostgreSQL with no time limit and no credit card requirement.

1. Navigate to [https://neon.tech](https://neon.tech) and sign up using your GitHub account.
2. Click **Create Project**:
   - **Project Name**: `leadintel-prod`
   - **Postgres Version**: 16 (default)
   - **Region**: Choose **US East (Ohio)** or **US East (N. Virginia)** to be geographically close to Render's Oregon region.
3. Neon automatically creates a default database named `neondb`. Create two isolated databases:
   - Navigate to **Databases** tab in the Neon console.
   - Click **New Database**, name it `lead_app`, and click **Create**.
   - Click **New Database**, name it `n8n`, and click **Create**.
4. Retrieve connection strings:
   - In the **Dashboard** or **Connection Details** pane, select database `lead_app` with connection pooling enabled.
   - Copy the connection string. It will look like:
     ```text
     postgresql://alex_owner:secret_password@ep-cool-fog-12345.us-east-2.aws.neon.tech/lead_app?sslmode=require
     ```
   - Select database `n8n` and copy its connection string:
     ```text
     postgresql://alex_owner:secret_password@ep-cool-fog-12345.us-east-2.aws.neon.tech/n8n?sslmode=require
     ```

Keep these two URLs handy for the next step.

---

## 4. Step 2: One-Click Deploy via Render Blueprint

Render Blueprints orchestrate all three containers (`leadintel-caddy`, `leadintel-api`, and `leadintel-n8n`) from the committed `render.yaml`.

1. Commit and push your changes to your GitHub repository:
   ```bash
   git add .
   git commit -m "feat(deploy): production deployment manifests"
   git push origin main
   ```
2. Navigate to [https://dashboard.render.com](https://dashboard.render.com) and log in with GitHub.
3. Click **New +** in the top navigation bar and select **Blueprint**.
4. Connect your repository: `salahadin-hassen/lead-intelligence-automation`.
5. Render will automatically detect `render.yaml` and configure the 3 services:
   - `leadintel-caddy`
   - `leadintel-api`
   - `leadintel-n8n`
6. Render will prompt you for the un-synced environment variables:
   - For `leadintel-api` → `DATABASE_URL`:
     Paste your Neon connection string for `lead_app`.
   - For `leadintel-n8n` → `N8N_DATABASE_URL`:
     Paste your Neon connection string for `n8n`.
   - For `leadintel-caddy` → `N8N_WEBHOOK_TARGET` & `leadintel-n8n` → `LEAD_API_BASE_URL`:
     Enter temporary placeholders (e.g. `https://placeholder.onrender.com`), or enter the exact Render URLs once assigned in the next step.
   - (Optional) `TELEGRAM_CHAT_ID`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_RECIPIENT_PHONE_NUMBER`:
     Leave blank for now if credentials are not yet configured; n8n will continue routing and persisting leads without notification errors.
7. Click **Apply**.
8. Render will build the Docker containers and deploy the services.

---

## 5. Step 3: Configure Service URLs & Credential Binding

Once Render creates your services, link their public URLs:

### 1. Set Public Service URLs in Render Dashboard
1. Note the assigned public URLs:
   - `leadintel-caddy`: `https://leadintel-caddy-xxxx.onrender.com`
   - `leadintel-api`: `https://leadintel-api-xxxx.onrender.com`
   - `leadintel-n8n`: `https://leadintel-n8n-xxxx.onrender.com`
2. Update **`leadintel-caddy`** → **Environment**:
   - `N8N_WEBHOOK_TARGET`: `https://leadintel-n8n-xxxx.onrender.com`
   - Click **Save Changes** (triggers quick restart).
3. Update **`leadintel-n8n`** → **Environment**:
   - `LEAD_API_BASE_URL`: `https://leadintel-api-xxxx.onrender.com`
   - Click **Save Changes**.
4. Retrieve the auto-generated `API_AUTH_TOKEN`:
   - Open **`leadintel-api`** → **Environment**.
   - Copy the value of `API_AUTH_TOKEN`.

### 2. Configure Native Header Auth Credential in n8n (One-Time Setup)
1. Open your n8n public URL in a browser:
   `https://leadintel-n8n-xxxx.onrender.com`
2. Complete the standard first-time owner account setup (Name, Email, Password).
3. Go to **Credentials** in the left sidebar and click **Add Credential**:
   - Search for **Header Auth** (`httpHeaderAuth`) and select it.
   - Credential Name: `FastAPI Bearer Auth`
   - Name: `Authorization`
   - Value: `Bearer <your_API_AUTH_TOKEN>` (paste the token copied from `leadintel-api`).
   - Click **Save**.
4. Go to **Workflows** in the left sidebar and open `lead-intake`:
   - Click on the node named `Create Lead in FastAPI`.
   - In the node properties, locate **Credential for Header Auth**.
   - Select your newly created `FastAPI Bearer Auth` from the dropdown list.
   - Click **Save** (top right of the workflow editor).
5. **Restart Safety Guarantee**:
   The container entrypoint checks if `lead-intake` already exists in the database. On routine container restarts or redeployments, it **preserves** the existing workflow and its bound credentials, re-registering the production webhook without overwriting your manual UI binding.

---

## 6. Step 4: Verification & Health Checks

### 1. Verify Application API Health
Open the public URL for `leadintel-api`:
```bash
curl -i https://leadintel-api-xxxx.onrender.com/health
```
**Expected Response**:
```json
HTTP/2 200
content-type: application/json

{"status":"ok"}
```
*(Confirms that the FastAPI service is healthy and the database pool successfully connected to Neon).*

### 2. Verify Website & Edge Proxy
Open the public URL for `leadintel-caddy` in your browser:
```text
https://leadintel-caddy-xxxx.onrender.com/
```
Verify that:
- The website loads with full responsive styling.
- The interactive demo form renders with the dynamic score dial.
- No console errors are present.

### 3. End-to-End Synthetic Lead Submission
Submit a synthetic test lead through either the web form or CLI:
```bash
curl -i -X POST https://leadintel-caddy-xxxx.onrender.com/webhook/lead-intake \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Jane Doe",
    "email": "jane@enterprise-tech.io",
    "company": "Enterprise Tech Corp",
    "message": "We need custom automated lead qualification for 25 sales reps urgently. Ready to implement next week.",
    "source": "portfolio-demo",
    "external_id": "demo-001"
  }'
```
**Expected Response**:
```json
HTTP/2 200
content-type: application/json

{
  "status": "received",
  "lead_id": 1,
  "score": 93,
  "score_reason": "High intent indicators (+30); Enterprise company domain (+18); Urgency signals detected (+15)",
  "qualification": "qualified"
}
```

### 4. Verify Duplicate Prevention
Repeat the exact same submission:
```bash
curl -i -X POST https://leadintel-caddy-xxxx.onrender.com/webhook/lead-intake \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Jane Doe",
    "email": "jane@enterprise-tech.io",
    "company": "Enterprise Tech Corp",
    "message": "We need custom automated lead qualification...",
    "source": "portfolio-demo",
    "external_id": "demo-001"
  }'
```
**Expected Response**:
```json
HTTP/2 409
content-type: application/json

{
  "detail": "Lead with external_id 'demo-001' from source 'portfolio-demo' already exists."
}
```

---

## 7. Notification Credentials (Optional)

To enable live Telegram and WhatsApp notifications on qualified leads:

1. In Render Dashboard, select `leadintel-n8n` → **Environment**.
2. Set:
   - `TELEGRAM_CHAT_ID`: Your personal or group chat ID.
   - `WHATSAPP_PHONE_NUMBER_ID`: Meta Cloud Phone Number ID.
   - `WHATSAPP_RECIPIENT_PHONE_NUMBER`: Recipient phone number in E.164 format.
3. In n8n's credential manager:
   - Add your Telegram Bot Token (`Telegram Lead Alerts`).
   - Add your WhatsApp Cloud API Bearer Token (`WhatsApp Lead Alerts`).
4. Changes take effect on the next lead intake.
