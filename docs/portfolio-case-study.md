# Case Study: Automated B2B Lead Qualification & Resilient Ingestion

> **How LeadIntel replaces manual inbox triage with an automated, database-backed intake pipeline featuring hybrid intent scoring, strict duplicate rejection, and decoupled sales routing.**

---

## 1. Executive Summary

In B2B sales and marketing operations, inbound contact forms are frequently managed through manual inbox monitoring or fragile, multi-step "no-code" automations. These approaches routinely suffer from delayed response times, duplicate records, subjective qualification, and silent data loss when third-party APIs fail.

**LeadIntel** is an inbound lead qualification and routing system engineered to address these operational bottlenecks with software engineering discipline. Capturing inquiries via a responsive web portal, the system validates inputs, enforces database-level uniqueness, evaluates purchase intent via a hybrid AI/heuristic scoring engine, and routes high-priority prospects to sales teams in real time.

The system is deployed on public cloud infrastructure (Render and Neon PostgreSQL) and verified through automated test suites, zero-loss failure drills, and production browser verification.

---

## 2. The Business Problem

Organizations investing in digital demand generation encounter four persistent operational challenges:

```
┌───────────────────────┐     ┌───────────────────────┐     ┌───────────────────────┐     ┌───────────────────────┐
│     Triage Latency    │     │  Duplicate Ingestion  │     │ Subjective Triage     │     │    Silent Failures    │
│ High-intent prospects │     │ Double-clicks & page  │     │ Reps evaluate leads   │     │ No-code zaps fail     │
│ wait hours or days for│     │ refreshes clutter data│     │ inconsistently; high- │     │ on timeouts; prospect │
│ initial sales contact │     │ & confuse ownership   │     │ value deals slip by   │     │ data lost with no log │
└───────────────────────┘     └───────────────────────┘     └───────────────────────┘     └───────────────────────┘
```

1. **Response Latency:** Inbound inquiries that sit in unmonitored inboxes or unassigned spreadsheets often lose momentum before a sales representative initiates contact.
2. **Duplicate Inquiries & Pipeline Clutter:** Prospects frequently refresh confirmation pages, click submit buttons repeatedly, or submit multiple forms across different campaigns. Without database-level uniqueness constraints, this results in duplicate records, overlapping sales outreach, and split deal attribution.
3. **Inconsistent Qualification:** When lead qualification depends solely on manual human review, evaluations vary by representative. Inquiries from personal email addresses may be pursued while subtle enterprise signals from corporate prospects go unnoticed.
4. **Fragile Automation Architecture:** Standard "no-code" workflow automations often lack defensive error boundaries. If an AI API times out or a notification service is temporarily down, the entire workflow can abort silently, losing the prospect's submission without notifying the visitor or preserving an audit trail.

**The Engineering Objective:** Build a lead ingestion pipeline that guarantees **no lead is lost**, **duplicates are rejected at the database layer**, **external service outages degrade gracefully**, and **sales teams are alerted immediately to high-intent opportunities**.

---

## 3. Implemented Solution & End-to-End Data Flow

LeadIntel separates presentation, ingestion, validation, and notification into dedicated architectural layers with clear boundaries:

```text
Visitor Browser
    │
    │  1. HTTPS Form Submission (with client-side submission key)
    ▼
Edge Reverse Proxy (Caddy 2.8)
    │
    │  2. Same-origin proxying to /webhook/lead-intake
    ▼
Orchestration Layer (n8n 2.41.7)
    │
    │  3. Forward payload with Header Authentication
    ▼
Application API (FastAPI / Python 3.12)
    ├── Schema validation via Pydantic (rejects invalid emails/empty fields: 422)
    ├── Duplicate rejection on (source, external_id) (returns 409 Conflict)
    ├── Parameterized SQL persistence into Neon PostgreSQL 16
    └── Multi-tier intent scoring engine:
           ├── Primary: OpenRouter LLM inference
           └── Fallback: Deterministic local heuristic scorer (keyword & domain signals)
    │
    │  4. HTTP 201 Created (with score, reason, and lead ID)
    ▼
n8n Routing & Decoupled Notification Engine
    ├── 5a. Immediate HTTP 201 response sent to visitor browser
    └── 5b. Parallel qualification & notification dispatch:
           ├── Score >= 70  ──► High Priority  ──► Instant Telegram / WhatsApp alert to sales
           ├── Score < 70   ──► Normal Lead    ──► Structured audit logging (no alerts)
           └── Score is NULL ─► Manual Review  ──► Flagged alert to sales management
```

### End-to-End Workflow Stages:

1. **Client-Side Ingestion (`demo/lead-form.html`):** The prospect submits an inquiry. The client script generates a unique `external_id` (a UUID or timestamped token), allowing the backend to detect duplicate submissions of the same inquiry.
2. **Edge Proxying (Caddy):** Inbound requests arrive at the same origin (`/webhook/lead-intake`). Caddy terminates TLS and proxies the request to the workflow orchestrator. Internal ports and private backend URLs are never exposed to the public internet.
3. **Validation & Persistence (FastAPI & PostgreSQL):** The n8n orchestrator passes the submission to FastAPI. The API validates all fields, checks for duplicate submissions, writes the record to PostgreSQL, and evaluates lead intent.
4. **Hybrid Intent Scoring:** The inquiry is scored on a scale from 0 to 100 based on message intent, buying signals, and corporate domain credibility.
5. **Caller-Response Isolation:** As soon as the lead is safely written to PostgreSQL, n8n transmits an HTTP 201 Created confirmation back to the visitor. Downstream notifications execute independently; a temporary outage in an external messaging service will not cause a false error for the prospect.
6. **Decoupled Operator Routing:** High-intent leads (`score >= 70`) trigger instant mobile alerts (Telegram Bot API and WhatsApp Business Cloud API). Lower-intent inquiries route quietly to structured audit logs without distracting sales representatives.

---

## 4. Technology Stack & Component Responsibilities

The architecture strictly adheres to a **Separation of Authority** pattern:

| Component | Technology | Primary Responsibility | Architectural Guarantee |
| :--- | :--- | :--- | :--- |
| **Edge Ingress** | Caddy 2.8 | HTTPS termination, static asset delivery, and reverse proxying. | Single public origin; hides all backend container ports and internal routes. |
| **User Interface** | HTML5 / Vanilla JS | Responsive marketing landing page and interactive lead demo. | Dependency-free, accessible, client-side submission key generation. |
| **Orchestration** | n8n 2.41.7 | Webhook reception, threshold routing, notification dispatch, and execution audit logs. | Workflow routing; zero database queries or domain calculations inside workflow nodes. |
| **Core API** | FastAPI (Python 3.12) | Schema validation, database transactions, duplicate checking, and scoring dispatch. | Fail-closed token authentication in production; all SQL queries parameterized. |
| **Primary Database** | Neon PostgreSQL 16 | Authoritative persistence for leads, scoring results, and status lifecycles. | Enforces database constraints: `UNIQUE (source, external_id)` and `CHECK` score bounds. |
| **Intent Scoring** | OpenRouter + Python | Evaluates buying intent (0–100) and rationale. | Automatic fallback to local heuristic engine if external inference times out or fails. |
| **Alerting** | Telegram + WhatsApp | Operator notifications for high-priority opportunities. | Node-level error boundaries (`continueErrorOutput`) isolate notification failures. |

---

## 5. Production Verification & Synthetic Testing

The system is deployed on public cloud infrastructure and was verified on release commit `2fc400f`. Testing was performed using synthetic B2B test data to evaluate end-to-end performance and reliability:

```text
[ Live Browser Form ] ──► [ Caddy ] ──► [ n8n ] ──► [ FastAPI ] ──► [ Neon PostgreSQL ]
       │                                                                  │
       └────────────── 201 Created (Observed round-trip: ~4.5s) ──────────┘
```

### Verified Test Results:

- **Deployment Environment:** Public web services hosted on Render (`leadintel-caddy.onrender.com`, `leadintel-api-2y0i.onrender.com`, `leadintel-n8n.onrender.com`) connected to a serverless Neon PostgreSQL cluster.
- **Form Submission Verification:** A synthetic test inquiry was submitted through the public website form to verify the live pipeline.
- **Observed Sample Latency:** In a verified live test run on warm instances, the complete round-trip—including edge routing, schema validation, database commit, scoring, and response generation—completed in **4,537 ms**. *(Note: This timing represents an individual observation on free-tier hosting; it is not a performance SLA or guaranteed speed).*
- **Scoring Output:** The system scored the synthetic lead (43/100) and returned a descriptive explanation: *"free-mail email domain; strong source (contact form)"*.
- **Database Persistence:** Direct inspection of Neon PostgreSQL confirmed that the lead row was persisted with exact matching fields, score, and status.
- **Workflow Execution:** Inspection of n8n execution history confirmed that the workflow completed successfully and routed to the normal (non-alerting) path because the score was below the qualification threshold of 70.

---

## 6. Database-Enforced Duplicate Protection (Conflict Detection)

Duplicate inquiries are a primary source of data contamination. LeadIntel addresses this with database-enforced deduplication:

```text
Second Submission (Identical external_id)
       │
       ▼
FastAPI checks UNIQUE constraint on (source, external_id)
       │
       ├── Duplicate detected in PostgreSQL
       ▼
HTTP 409 Conflict returned to caller (Observed sample timing: ~4.5s)
       │
       ├── UI renders: "Duplicate Ingestion Blocked (409)"
       └── Database state: Exactly 1 row in table (duplicate insert rejected)
```

### Test Execution & Outcome:

1. **Replay Action:** Immediately following the initial submission, the identical synthetic payload was resubmitted using the demo portal's duplicate replay mechanism.
2. **System Response:** Rather than creating a duplicate record or silently overwriting data, the intake pipeline detected the existing `(source, external_id)` combination and returned **HTTP 409 Conflict** (observed in 4,529 ms during test verification).
3. **User Experience:** The browser form displayed a clear warning panel: *"Duplicate Ingestion Blocked (409) — A lead with this source and external_id already exists"*.
4. **Database Verification:** Direct inspection of the Neon PostgreSQL database confirmed that **exactly one record** remained. The secondary insert was blocked by the database engine's unique constraint.
5. **Workflow Execution:** Recorded in n8n execution history with status `success`, demonstrating that the duplicate response was handled cleanly as a standard operational branch rather than an unhandled application failure.

---

## 7. Security & Operational Controls Implemented

Security and data integrity measures were verified across the production deployment:

1. **Ingress Abstraction & Surface Minimization:**
   - The public website communicates exclusively with same-origin `/webhook/lead-intake` on the Caddy edge proxy.
   - Internal microservice URLs, private container hostnames, and database ports are never exposed to the client browser.
   - The public frontend configuration file (`config.js`) contains only the relative path `{"webhookUrl": "/webhook/lead-intake"}` and exposes zero API keys, secrets, or internal identifiers.
2. **Fail-Closed API Authentication:**
   - In production (`APP_ENV=production`), the FastAPI backend requires a secure Bearer token (`API_AUTH_TOKEN`) for `/leads*` operations.
   - Direct requests to backend endpoints without valid authentication return **HTTP 401 Unauthorized**. The public `/health` endpoint remains open for container orchestration health checks without exposing sensitive data.
3. **Encrypted Transport & Strict Certificate Verification:**
   - Database traffic between backend containers and Neon PostgreSQL is encrypted via TLS (`sslmode=require`).
   - Container connection pools enforce certificate validation against trusted root certificate authorities (ISRG Root X1).
4. **PII Minimization in External AI Prompts:**
   - When submitting lead data to external LLM providers for scoring, the payload is sanitized to include only `source`, `company`, and `message`.
   - Contact coordinates (`name` and `email`) are omitted before inference to prevent unnecessary personal data transmission.
5. **Caller-Response Isolation & Error Boundaries:**
   - The visitor's HTTP confirmation is delivered before downstream notification nodes execute.
   - Outages or rate limits in third-party messaging services (Telegram or Meta WhatsApp API) cannot cause an HTTP 500 error for a lead that was successfully saved in the database.

---

## 8. Current Limitations, Operational Risks & Scope Boundaries

To maintain technical accuracy, the system's operational constraints and current scope boundaries are explicitly documented:

- **Free-Tier Cold Starts:**
  The demonstration environment is hosted on Render's free tier, where container instances spin down after 15 minutes of inactivity. When a request arrives while containers are asleep, the initial cold-start boot takes ~40–50 seconds. Warm requests execute in ~4–5 seconds. The client-side form includes a 120-second connection timeout to accommodate free-tier container wake-up delays without the browser aborting prematurely. This timeout is an operational accommodation for hosting sleep cycles, not a measure of system throughput. Commercial production deployments on dedicated containers eliminate cold starts entirely.
- **External AI Rate Limits:**
  The primary scoring engine uses OpenRouter's free router, which enforces daily request quotas (50 requests/day). If the quota is exceeded or the external API times out, the local deterministic heuristic scorer takes over automatically, ensuring lead intake never halts.
- **Static Qualification Threshold:**
  The qualification score threshold ($70$) is an operational rule based on observed heuristic signal clustering; it is not a machine learning model trained on historical closed-won conversion data.
- **WhatsApp Live Delivery Dependency:**
  The n8n workflow includes complete template schemas, payload formatting, and error-handling logic for the WhatsApp Business Cloud API. However, live message delivery requires an active Meta Developer account, approved business message templates, and verified recipient phone numbers (documented in [`docs/whatsapp-setup.md`](whatsapp-setup.md)).
- **Explicit Scope Boundaries (Not Implemented):**
  This project is an intake and qualification MVP. It does not include bidirectional CRM synchronization (e.g., Salesforce/HubSpot bidirectional syncing), email inbox scraping (e.g., Gmail API monitoring), a multi-tenant user authentication portal, or asynchronous message broker infrastructure (e.g., Celery/RabbitMQ).

---

## 9. Adaptation Guide for Client Workflows

This architecture serves as an adaptable foundation for custom B2B intake pipelines. Key areas that can be tailored for client-specific requirements include:

1. **Tailored Scoring Criteria:**
   The heuristic rules and LLM system prompts can be adjusted to reflect a client's Ideal Customer Profile (ICP), weighting specific target industries, budget thresholds, software toolchains, or geographic requirements.
2. **Custom Notification & Downstream Channels:**
   The n8n routing layer can be extended to dispatch alerts to Slack channels, Microsoft Teams webhooks, Google Sheets, or directly create records inside CRMs such as HubSpot, Salesforce, or Pipedrive.
3. **Multi-Source Intake Consolidation:**
   In addition to website contact forms, the same pipeline can ingest leads from Typeform, Calendly booking webhooks, LinkedIn Lead Gen forms, or inbound marketing landing pages by standardizing them against the existing `(source, external_id)` contract.
4. **Dedicated Infrastructure Upgrades:**
   For high-volume commercial operations, the deployment can be migrated to persistent container infrastructure (such as AWS ECS, GCP Cloud Run, or paid Render instances) to eliminate cold starts and provide automatic horizontal autoscaling.
