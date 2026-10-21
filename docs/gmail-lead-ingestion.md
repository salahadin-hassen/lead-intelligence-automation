# LeadIntel — Gmail Lead Ingestion & Automated Qualification

This guide provides technical specifications, operational procedures, security boundaries, and troubleshooting steps for the LeadIntel Gmail ingestion channel.

---

## 1. Architectural Overview & Ingestion Flow

The Gmail ingestion pipeline introduces an asynchronous email triage channel alongside the website lead form, feeding normalized prospect inquiries directly into LeadIntel's scoring and persistence core:

```
+-------------------------------------------------------------------------+
|                              PILOT MAILBOX                              |
|   Inbound Customer Email --> Labeled "LeadIntel_Leads"                  |
+-------------------------------------------------------------------------+
                                    |
                                    | Poll (every 1 min, readStatus: "all")
                                    v
+-------------------------------------------------------------------------+
|                       N8N WORKFLOW ENGINE (v2.41.7)                     |
|                                                                         |
|   1. Gmail Trigger (filters: LeadIntel_Leads, -in:spam -in:trash)       |
|   2. Normalize Email Lead (Code Node):                                  |
|      - Extract Gmail message ID -> external_id                          |
|      - Extract sender name & email from From header                     |
|      - Detect & skip automated bounces / out-of-office                  |
|      - Strip HTML tags & decode basic entities                          |
|      - Strip quoted reply chains ("> ...", "On ... wrote:")             |
|      - Infer company from domain or assign "Unspecified"                |
|      - Format composite message: "Subject: <subj>\n\n<cleaned body>"    |
|   3. Filter Valid Leads (Switch: valid vs skipped)                      |
|   4. HTTP Request (POST to FastAPI /leads with Header Auth token)       |
|   5. Route on API Status:                                               |
|      * 201 Created   -> Extract Lead + Score -> Qualification           |
|      * 409 Duplicate -> Log Idempotent Duplicate (No error)             |
|      * 422 Invalid   -> Log Validation Failure (Review branch)          |
|      * 401/403 Auth  -> Log Auth Error (Alert operator)                 |
|      * 5xx Upstream  -> Log Upstream Error                              |
+-------------------------------------------------------------------------+
                                    |
                                    | Authenticated POST (Bearer Token)
                                    v
+-------------------------------------------------------------------------+
|                           FASTAPI CORE BACKEND                          |
|                                                                         |
|   - Pydantic validation (LeadCreate)                                    |
|   - Database unique constraint check: UNIQUE(source, external_id)        |
|   - Deterministic & heuristic intent scoring (0-100)                    |
|   - PostgreSQL persistence (status: "new", score, score_reason)         |
+-------------------------------------------------------------------------+
                                    |
                                    v
+-------------------------------------------------------------------------+
|                        NEON POSTGRESQL (lead_app)                       |
|   Table: leads                                                          |
|   Record: { source: 'gmail', external_id: '<msgId>', status: 'new' }   |
+-------------------------------------------------------------------------+
```

---

## 2. Safe Mailbox Scope & Security Guardrails

To protect client privacy and ensure mailbox integrity, the integration adheres to strict defensive design principles:

1. **Explicit Boundary Only (`LeadIntel_Leads`):**
   - The workflow never scans or ingests an entire inbox.
   - Only messages bearing the dedicated label `LeadIntel_Leads` are queried.
   - Spam and Trash folders are strictly excluded (`-in:spam -in:trash`).
2. **Read-Only Operation:**
   - The workflow **never** sends, replies, forwards, archives, deletes, or modifies emails.
   - It does not alter message read/unread flags.
3. **Read-State Independence (`readStatus: "all"`):**
   - By setting `readStatus: "all"`, a sales representative can open and read an inquiry in their email client without causing the automation to skip the message on the next polling cycle.
4. **No Attachment Processing:**
   - In this MVP iteration, email attachments are not downloaded or parsed (`downloadAttachments: false`), eliminating malicious payload ingestion risks.
5. **Untrusted Data Handling:**
   - Email text is treated as untrusted string content. No code or script within an email body is ever evaluated.
   - Large messages are capped at 3,000 characters to prevent database bloat or Denial-of-Service vectors.

---

## 3. Dedicated Gmail Label Setup

### Step 1: Create the Label in Gmail
1. Log into the pilot Gmail account.
2. In the left sidebar, click the **+** icon next to **Labels** (or go to **Settings** $\rightarrow$ **Labels** $\rightarrow$ **Create new label**).
3. Name the label exactly:
   ```text
   LeadIntel_Leads
   ```
4. Click **Create**.

### Step 2: (Optional) Configure an Inbound Gmail Filter
To automate label assignment for inbound sales leads without manual tagging:
1. In Gmail, click the search options slider icon on the right side of the search bar.
2. In the **To** field, enter your public sales address (e.g. `sales@yourcompany.com`).
3. In the **Includes the words** field, add sales keywords (e.g. `inquiry OR pricing OR demo OR trial OR quote`).
4. Click **Create filter**.
5. Select:
   - **Apply the label:** Choose `LeadIntel_Leads`.
   - **Never send it to Spam**.
6. Click **Create filter**.

---

## 4. Normalization Rules & Field Mapping

The normalization Code node (`n8n/normalize-gmail.js`) converts irregular MIME email data into LeadIntel's strict `LeadCreate` contract:

| Target Field | Source Extraction / Derivation | Fallback / Missing-Value Policy |
| :--- | :--- | :--- |
| `name` | Extracted display name from `From` header (e.g., `"Jane Doe <jane@co.com>"` $\rightarrow$ `Jane Doe`). | If no display name exists, parsed from local-part (e.g., `jane.smith@...` $\rightarrow$ `Jane Smith`). If non-alphabetic or empty $\rightarrow$ `"Direct Inquirer"`. |
| `email` | Validated email address from `From` header. | Malformed or missing email causes immediate routing to `Log Skipped Message` (never sent to FastAPI). |
| `company` | Inferred corporate name from corporate email domain (e.g., `alice@acme-corp.com` $\rightarrow$ `Acme Corp`). | For free-mail providers (`gmail.com`, `yahoo.com`, `outlook.com`, etc.): parsed from `Company: ...` in message body if present; otherwise set to `"Unspecified"`. |
| `message` | Extracted plain-text body; if HTML only, converted to plain text with HTML tags removed and basic entities decoded. Quoted replies (`> ...`, `On ... wrote:`) stripped. | Prefixed with `Subject: <subject>\n\n`. Truncated at 3,000 characters. If body is empty: `Subject: <subject>` or `[Inbound email inquiry with no text body]`. |
| `source` | Hardcoded: `"gmail"`. | Required by API and scoring. |
| `external_id`| Gmail's stable internal message ID (e.g. `18c9a1f2e3d4b5a6`). | Guarantees database deduplication via `UNIQUE(source, external_id)`. |

### Automated Notice Rejection
Before payload construction, the normalization engine checks:
- `auto-submitted: auto-replied` or `auto-generated` headers
- `mailer-daemon@`, `postmaster@`, `noreply@` sender addresses
- `Automatic reply:`, `Out of Office:`, `Delivery Status Notification`, `Undelivered Mail` subjects
Matching items are immediately routed to `Log Skipped Message` with reason `automated_notice` and do not consume database or scoring resources.

---

## 5. Scoring & Idempotency Semantics

### Neutral Scoring Behavior
Under LeadIntel's heuristic scoring engine (`app/scoring.py`), leads originating from `source="gmail"` are treated as neutral inbound opportunities:
- No artificial score inflation is granted simply for arriving via email.
- The lead receives the baseline score (50 points), adjusted by:
  - High-intent sales keywords in subject/body (+10 to +30 points)
  - Corporate email domain (+10 points) vs. free webmail (-10 points)
  - Substantial inquiry length $\ge 100$ characters (+10 points) vs. short message $< 20$ characters (-15 points)
- The score reason reflects the evaluation transparently: e.g. `High-intent keywords (demo, pricing); corporate email domain; direct email channel (gmail)`.

### Idempotency on Replayed Messages
- When the Gmail Trigger re-polls or reprocesses an already-ingested message, FastAPI detects the duplicate `(source='gmail', external_id=messageId)` composite key and returns `HTTP 409 Conflict`.
- In `n8n/gmail-lead-intake.json`, the `Route on API Status` switch routes 409 responses to `Log Idempotent Duplicate`.
- This is classified as an **expected idempotent success**, preventing duplicate rows in PostgreSQL and avoiding workflow failure alerts.

---

## 6. Step-by-Step Google Cloud OAuth 2.0 Setup

To connect a pilot Gmail inbox to self-hosted n8n, configure Google Cloud credentials:

### Step 1: Create a Google Cloud Project & Enable Gmail API
1. Navigate to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (e.g., `leadintel-automation-pilot`).
3. In the search bar, search for **Gmail API** and click **Enable**.

### Step 2: Configure the OAuth Consent Screen
1. Go to **APIs & Services** $\rightarrow$ **OAuth consent screen**.
2. Select **External** (unless you are using a Google Workspace tenant with internal users only) and click **Create**.
3. Fill in:
   - **App name:** `LeadIntel Gmail Ingestion`
   - **User support email:** Your email address
   - **Developer contact information:** Your email address
4. Click **Save and Continue**.
5. On the **Scopes** page, click **Add or Remove Scopes**:
   - Manually add the read-only scope:
     ```text
     https://www.googleapis.com/auth/gmail.readonly
     ```
   *(Do NOT add sensitive modify, compose, or delete scopes).*
6. Click **Save and Continue**.
7. On the **Test users** page, click **+ Add Users** and enter the exact Gmail address of your pilot inbox.
8. Click **Save and Continue**.

### Step 3: Create OAuth 2.0 Client Credentials
1. Go to **APIs & Services** $\rightarrow$ **Credentials**.
2. Click **+ Create Credentials** $\rightarrow$ **OAuth client ID**.
3. Set **Application type** to **Web application**.
4. Name: `LeadIntel n8n Pilot Client`.
5. Under **Authorized redirect URIs**, enter the exact callback URL from your deployed n8n instance:
   ```text
   https://leadintel-n8n.onrender.com/rest/oauth2-credential/callback
   ```
   *(Confirm this redirect URI inside your n8n Credential creation modal).*
6. Click **Create**.
7. Copy the generated **Client ID** and **Client Secret**.

### Step 4: Add Credentials in n8n UI
1. Log into your n8n dashboard (`https://leadintel-n8n.onrender.com`).
2. Navigate to **Credentials** $\rightarrow$ **Add Credential** $\rightarrow$ search for **Gmail OAuth2 API**.
3. Paste the **Client ID** and **Client Secret**.
4. Click **Connect my account** and complete Google's authorization prompt using the pilot test user account.
5. Save the credential with the name: `Gmail account` (credential ID `gmail_oauth_pilot`).

---

## 7. Workflow Deployment & Activation Procedure

The Gmail workflow artifact is version-controlled at `n8n/gmail-lead-intake.json`.

1. **Non-Destructive Container Import:**
   - `deploy/docker/Dockerfile.n8n` bundles both `lead-intake.json` and `gmail-lead-intake.json`.
   - On container boot, `deploy/docker/n8n-entrypoint.sh` checks if `gmail-lead-intake` exists in the database.
   - If not found, it imports the workflow in an **INACTIVE** state.
   - If it already exists, the entrypoint preserves the existing record, protecting manual OAuth token bindings from being overwritten on container restart.
2. **Controlled Verification Before Activation:**
   - Open the workflow `Gmail Lead Intake & Qualification` in n8n.
   - Ensure the `Gmail Trigger` node references your newly authorized `Gmail account` credential.
   - Click **Test step** on the `Gmail Trigger` with a test email labeled `LeadIntel_Leads` in your mailbox.
   - Verify that `Normalize Email Lead` outputs a clean `lead` payload.
3. **Manual Activation:**
   - Once end-to-end execution succeeds in test mode, toggle the workflow switch to **Active**.

---

## 8. Render Free Tier Idle Sleeping Analysis & Polling Constraints

### The Sleep Constraint
- Render Free Web Services automatically sleep after **15 minutes of inactivity** (zero inbound HTTP requests).
- The `n8n-nodes-base.gmailTrigger` operates on an internal timer loop (`pollTimes: everyMinute`).
- **When the n8n container is sleeping, internal polling stops.** No emails are ingested while the container is in a suspended state.
- When an inbound HTTP request wakes the container (e.g. a website visitor loading the site or submitting the demo form, or an operator opening the n8n dashboard), n8n undergoes a ~40–50 second cold start.
- Upon waking, the Gmail Trigger immediately checks the mailbox and processes all messages with the `LeadIntel_Leads` label that arrived while the service was asleep.

### Architectural Assessment
| Requirement | Status on Render Free | Note |
| :--- | :--- | :--- |
| **Real-time 24/7 background polling** | **Not Guaranteed** | Polling pauses during 15-min idle sleep intervals. |
| **Batch catch-up upon container wake** | **Verified & Operational** | All unpolled labeled emails are ingested upon wake-up. |
| **Zero infrastructure cost** | **Compliant** | Fits within the free tier budget. |

### Recommended Production Next Steps
1. **Google Cloud Pub/Sub Webhook Push (Recommended Free Architecture):**
   - Configure a Gmail push notification watch via Google Cloud Pub/Sub that forwards incoming mail events to an n8n webhook URL.
   - Because Render Web Services wake up automatically on incoming HTTP requests, a push webhook wakes n8n instantly whenever an email arrives, completely eliminating the polling sleep limitation without paying for hosting.
2. **Always-On Compute Tier:**
   - Upgrading the `leadintel-n8n` service to Render Starter ($7/month) keeps the instance permanently awake with zero cold starts and continuous 1-minute polling.

---

## 9. Google OAuth Testing Mode Expiration Notice

When an OAuth consent screen is configured with **Publishing status: Testing**:
- **7-Day Token Expiry:** Google Cloud automatically revokes OAuth refresh tokens after **7 consecutive days**.
- **Impact:** Once a week, the pilot inbox will disconnect, requiring the operator to re-authenticate via the n8n UI.
- **Production Resolution:**
  - For internal Google Workspace organizations, change user type to **Internal** (refresh tokens do not expire).
  - For external production multi-client use, submit the app for Google App Verification.
