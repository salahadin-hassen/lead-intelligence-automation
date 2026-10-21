# Lead Intelligence Automation

> An automated inbound lead qualification and routing system built with FastAPI, PostgreSQL, n8n, configurable LLM scoring, and dual Telegram/WhatsApp operator alerts.

**Live Production Deployment:** [https://leadintel-caddy.onrender.com](https://leadintel-caddy.onrender.com)  
**Interactive Lead Demo:** [https://leadintel-caddy.onrender.com/demo](https://leadintel-caddy.onrender.com/demo)  
**Production Release Status:** Verified on commit [`2fc400f`](https://github.com/salahadin-hassen/lead-intelligence-automation/commit/2fc400f) (All 14 deployment gates passed)

---

## 30-Second Project Overview (For Clients & Recruiters)

| Question | Detail |
| :--- | :--- |
| **What does it do?** | Ingests B2B contact inquiries, rejects duplicate submissions at the database level, scores buying intent ($0\text{--}100$) using hybrid AI, and alerts sales teams in real time. |
| **Live Product Demo** | Hosted live at [`https://leadintel-caddy.onrender.com`](https://leadintel-caddy.onrender.com). Dedicated `/demo` route with instant form validation and duplicate replay testing. |
| **Production Architecture** | Caddy 2.8 Edge Ingress $\rightarrow$ n8n 2.41 Webhook $\rightarrow$ Authenticated FastAPI (Python 3.12) $\rightarrow$ Neon Serverless PostgreSQL 16. |
| **Verified Guarantees** | Zero duplicate leads (database unique constraints), multi-tier AI intent scoring with deterministic heuristic fallback, caller-response isolation, and **104 automated tests** (0 skips, 0 xfails). |
| **What is Implemented?** | Production-ready website lead intake and pilot Gmail lead intake (`LeadIntel_Leads` label boundary), database deduplication, intent scoring, Telegram sales alerts, and WhatsApp Cloud API templates. *(CRM sync and multi-tenant OAuth platform are planned for future phases).* |

---

## Portfolio Summary (Quick Read)

Inbound B2B lead generation often stalls on manual triage: validating contact details, checking for duplicate inquiries across campaigns, scoring purchase intent, and manually alerting sales reps. **Lead Intelligence Automation** is an automated pipeline engineered to solve this end-to-end.

Incoming inquiries from a responsive, client-facing web portal are filtered through access-controlled ingress, ingested by an **n8n** automation workflow, and persisted in **PostgreSQL 16** via a **FastAPI** service. Inbound messages are evaluated by a provider-agnostic LLM scoring engine (OpenRouter free router or OpenAI-compatible) that falls back automatically to a local, deterministic heuristic scorer if external inference is unavailable or malformed. High-priority leads (`score >= 70`) trigger instant Telegram and WhatsApp Business Cloud notifications to sales, while all leads receive structured, execution-native audit logging.

Engineered with defensive failure isolation: upstream intake never fails due to scoring or notification outages. Fully verified with **104 automated tests** (fail-loud test database requirement), comprehensive rehearsal failure drills, and workflow-level notification error boundaries.

---

## The Business Problem

When inbound prospective customers request a demo or submit a contact inquiry, companies face four operational bottlenecks:
1. **Slow Response Times:** Sales teams often wait hours or days for someone to manually review an inbox before contacting a high-intent prospect.
2. **Duplicate Ingestion:** Prospects submit multiple forms or refresh pages, creating duplicate CRM records and confusing account ownership.
3. **Inconsistent Qualification:** Different team members evaluate lead quality subjectively, letting high-value enterprise inquiries slip through while chasing low-intent inquiries.
4. **Fragile Automation:** Typical "no-code" automations break silently: an API error or webhook failure loses the lead record completely without an audit trail.

This system automates ingestion, deduplication, scoring, qualification, and sales notification with strict software engineering guarantees: **no lead is lost, duplicates are rejected at the database level, and external service failures degrade gracefully.**

---

## End-to-End Flow

```text
Lead Form (demo/lead-form.html) OR Gmail Inbound (Label: LeadIntel_Leads)
    │                                     │
    ▼ HTTPS (/webhook/lead-intake)        ▼ n8n Gmail Trigger (every 1 min, readStatus: all)
n8n Website Webhook Workflow          n8n Gmail Lead Intake Workflow (n8n/gmail-lead-intake.json)
    │                                     │ (Strip HTML, clean reply chains, extract sender)
    └──────────────────┬──────────────────┘
                       ▼ HTTP POST /leads (Authenticated with Bearer Token)
FastAPI Backend
    ├── Pydantic validation (422 on invalid schema / email)
    ├── Deduplication on (source, external_id) -> 409 Conflict (idempotent for Gmail)
    ├── PostgreSQL 16 persistence (raw parameterized SQL)
    └── Scoring Dispatcher
           ├── Primary: OpenRouter Free LLM (/chat/completions)
           └── Fallback: Deterministic local heuristic scorer
    │
    ▼ Response (201 Created with score & reason)
n8n Qualification & Audit Layer
    ├── score >= 70  ──► Qualified (Priority: High)   ──► Telegram & WhatsApp Alerts
    ├── score <  70  ──► Not Qualified (Priority: Normal) (Operational audit log only)
    └── score is null ──► Manual Review (Unscored)     ──► Telegram & WhatsApp Review Alerts
    │
    └──► Build Unified Audit Event (Persisted in n8n execution data)
```

> **Gmail Integration Reference:** Complete OAuth setup instructions, Google Cloud consent screen configuration, dedicated label filtering (`LeadIntel_Leads`), normalization rules, and Render Free idle sleeping limitations are documented in [`docs/gmail-lead-ingestion.md`](docs/gmail-lead-ingestion.md).

---

## Engineering Highlights

- **Separation of Concerns:** FastAPI owns data integrity, SQL queries, schema validation, and scoring. n8n owns workflow orchestration, routing decisions, audit event assembly, and multi-channel notification dispatch. Scoring logic is never duplicated in n8n.
- **Database-Enforced Invariants:** PostgreSQL `CHECK` constraints enforce score bounds (`0 <= score <= 100`) and status vocabulary (`new`, `contacted`, `qualified`, `closed`). A database `UNIQUE (source, external_id)` constraint prevents race-condition duplicates.
- **Defensive Multi-Tier Scoring:** LLM scoring parses strict JSON schemas. If the provider times out ($>5\text{ s}$), errors, or returns non-integer scores, the system seamlessly falls back to an offline heuristic analyzer. If both fail, `score` degrades cleanly to `NULL` without blocking lead intake.
- **Caller-Response Isolation:** In n8n, the HTTP `201 Created` response executes *before* the downstream qualification, audit, Telegram, and WhatsApp nodes. A downstream outage (such as Telegram or Meta WhatsApp API downtime) never delays or causes a false 500 error for the prospect.
- **Dual-Channel Mobile Alerts with Isolated Error Boundaries:** Priority (`qualified`) and `manual_review` leads trigger Telegram messages and WhatsApp Business Cloud API alerts. Both WhatsApp send nodes configure `onError: continueErrorOutput` to prevent notification failures from terminating sibling branches or executions.
- **PII Protection & Data Minimization:** Prompts sent to external LLMs contain only `source`, `company`, and `message` (customer `name` and `email` are never transmitted). WhatsApp notifications send only contact coordinates and omit free-text messages to minimize external exposure.
- **Responsive B2B Web Experience:** The client-facing demo portal is built in self-contained, dependency-free HTML5/CSS/JS with accessible form states, client-side validation, live payload preview telemetry, and dynamic `external_id` generation for duplicate prevention.
- **Strict Testing Philosophy:** 104 automated tests with zero skips and zero xfails. Tests require a real PostgreSQL database instance and fail loudly with descriptive errors if it is missing.

---

## Reliability Evidence

The system's reliability guarantees have been verified across automated unit/integration tests, local rehearsal-stack failure drills, and temporary public-demo browser sessions:

| Scenario | Expected Behavior | Evidence Level | Verification Detail |
|---|---|:---:|---|
| **Valid Lead Ingestion** | Lead validated, persisted in PostgreSQL, returns HTTP 201 with ID and score | **Automated & Public** | `pytest` integration test + public browser test (Lead ID 5, score 100) |
| **Duplicate External ID** | Database unique constraint triggers HTTP 409 Conflict; no duplicate row | **Automated & Public** | `pytest` + public submission with reused ID (`Respond Duplicate` panel) |
| **Invalid Payload** | Pydantic validation fails; returns HTTP 422 with field-level errors | **Automated & Public** | `pytest` + public form submit with invalid fields (HTTP 422) |
| **FastAPI Unavailable** | n8n catches connection failure and returns HTTP 502 Upstream Error | **Local Drill** | Rehearsal drill with stopped API process; caller received 502 with clean JSON |
| **LLM Provider Outage** | Automatically falls back to deterministic local heuristic scorer; lead created | **Automated & Local** | `pytest` (mocked timeouts & HTTP 429/503) + rehearsal run with bad credentials |
| **Both Scorers Fail** | Lead persisted with `score: null`; routes to `manual_review` in n8n | **Automated & Local** | `pytest` + live n8n execution routing unscored lead to manual review branch |
| **Qualified Telegram Alert** | Score $\ge 70$ triggers `Log Priority Route` and Telegram message delivery | **Local & Public** | Execution 135 confirmed Telegram Bot API returned `ok: true`, `message_id: 25` |
| **Qualified WhatsApp Alert** | Score $\ge 70$ triggers `Send Qualified to WhatsApp` with error boundary | **Workflow Verified** | Graph structure, parameterization, and `onError: continueErrorOutput` verified (live Meta delivery requires operator credentials) |
| **Manual Review WhatsApp Alert** | Score null triggers `Send Manual Review to WhatsApp` with score status | **Workflow Verified** | Graph routing verified; null score passed as "unavailable"; free-text body sanitized |
| **WhatsApp Node Failure** | Meta API rejection or bad credentials caught by `continueErrorOutput` | **Workflow Verified** | Error routes to failure log; HTTP 201, audit record, and Telegram alerts unaffected |
| **Unqualified Lead** | Score $< 70$ routes to `Log Normal Route`; no notification sent | **Local & Public** | Execution 136 confirmed score 27 executed normal route with zero Telegram/WhatsApp sends |
| **Telegram API Outage** | Intake returns 201; audit event written; failure visible in n8n execution log | **Local Drill** | Rehearsal drill with invalid chat ID; caller received 201, execution marked error |
| **Unauthenticated Ingress** | Caddy reverse proxy rejects unauthenticated requests with HTTP 401 | **Public Demo** | Public tunnel probe: unauthenticated GET and POST returned 401 without n8n run |

---

## Architecture & Responsibilities

| Component | Technology | Primary Responsibilities |
|---|---|---|
| **Edge / Proxy** | Caddy v2 | HTTPS termination, demo Basic Auth protection, same-origin static file serving, and `/webhook/*` reverse proxying. |
| **Client Portal** | Responsive Vanilla JS / HTML5 | Accessible, responsive product demo (`demo/lead-form.html`). Generates per-message `external_id` for duplicate prevention with live telemetry. |
| **Orchestrator** | n8n 2.41.7 | Webhook intake, API forwarding, score threshold comparison, structured audit event creation, Telegram Bot and WhatsApp Business dispatch. |
| **Core Service** | FastAPI (Python 3.12) | Request validation, business logic, connection pool lifecycle, and CSV export. Raw parameterized SQL only. |
| **Persistence** | PostgreSQL 16 | Authoritative data store. Manages `leads` table, indexes, unique constraints, and schema migrations. |
| **AI Inference** | OpenRouter / OpenAI API | Evaluates purchase intent and outputs a structured integer score ($0\text{--}100$) and reason. |
| **Heuristic Scorer**| Python stdlib | Pure, deterministic function evaluating intent keywords, email domain, message length, and source weight. |
| **Notification Bus**| Telegram + WhatsApp Cloud API | Decoupled operator notifications for qualified and manual-review leads with isolated error boundaries. |

---

## Implemented MVP vs. Planned Features

To maintain engineering credibility with clients and technical recruiters, the repository explicitly separates what is **fully implemented and verified** from **future roadmap items**:

| Capability Area | Implemented & Production-Verified MVP | Planned / Future Roadmap |
| :--- | :--- | :--- |
| **Inbound Capture** | Dedicated web portal (`/`) and interactive demo route (`/demo`) with client-side submission key generation (`external_id`). | Ingestion from external forms (Typeform, Calendly, LinkedIn ads). |
| **Edge Ingress** | Caddy 2.8 reverse proxy handling TLS and same-origin `/webhook/lead-intake` routing. | Custom enterprise vanity domains with automated SSL rotation. |
| **Data Validation** | FastAPI + Pydantic schema validation (422 Unprocessable Entity on malformed payloads). | Dynamic client-defined schema validation via UI builder. |
| **Data Deduplication** | Database-enforced `UNIQUE (source, external_id)` constraint returning HTTP 409 Conflict. | Fuzzy cross-campaign prospect matching and merge rules. |
| **Intent Scoring** | Hybrid multi-tier engine: OpenRouter LLM + deterministic heuristic fallback. | Fine-tuned domain models and adaptive ML qualification thresholds. |
| **Caller Isolation** | HTTP 201 response delivers before downstream notification nodes run. | Asynchronous background broker (Celery / RabbitMQ) for batch ingestion. |
| **Sales Notifications** | Telegram Bot API live alerts + WhatsApp Cloud API template routing with error boundaries. | Bidirectional CRM synchronization (HubSpot, Salesforce, Pipedrive). |
| **Deployment** | Dockerized deployment on Render Web Services + Neon Serverless PostgreSQL 16. | Multi-tenant SaaS architecture with user login & RBAC dashboard. |

---

## Known Scope & Operational Boundaries

- **Qualification Threshold:** The qualification threshold ($70$) is an initial operational business rule based on observed heuristic score clustering; it is not a statistically validated machine learning conversion model.
- **Production Deployment:** Live in production on Render free tier (`leadintel-caddy.onrender.com`, `leadintel-api-2y0i.onrender.com`, `leadintel-n8n.onrender.com`) and Neon Serverless PostgreSQL. Instances sleep after 15 minutes of inactivity; initial cold starts take ~40–50s, while warm requests execute in ~4–5s.
- **Live WhatsApp Delivery:** Workflow routing, template definitions, and isolated error boundaries are fully implemented in `n8n/lead-intake.json`. Live message delivery requires an active Meta Developer account, verified sender/recipient phone numbers, and approved message templates (see [`docs/whatsapp-setup.md`](docs/whatsapp-setup.md)).
- **Access Control Scope:** Ingress protection via Caddy; FastAPI implements fail-closed Bearer token authentication (`API_AUTH_TOKEN`) in production (`APP_ENV=production`).
- **Free LLM Provider Constraints:** OpenRouter free router availability depends on third-party daily quotas (50 requests/day). If exhausted or unavailable, the deterministic heuristic scorer takes over automatically.

---

## Quickstart & Local Verification

### Prerequisites
- Python 3.12+
- PostgreSQL 16 (running with a role and database, e.g., `lead_app` / `lead_automation`)

### 1. Installation
```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[test]"
```

### 2. Configuration
```bash
cp .env.example .env
# Edit DATABASE_URL and TEST_DATABASE_URL in .env
```

### 3. Run Automated Tests
```bash
.venv/bin/python -m pytest -q
# Expected result: 104 passed
```

### 4. Run the API Locally
```bash
.venv/bin/uvicorn app.main:app --reload --port 8000
# Migrations in sql/ apply automatically on boot
# Health check: curl http://localhost:8000/health
```

---

## Documentation Quick Links

- [**Client Portfolio Case Study**](docs/portfolio-case-study.md) — Comprehensive business case study, architectural decisions, and production verification evidence.
- [**Client Live Demo Guide**](docs/client-demo-guide.md) — 3-to-5 minute repeatable live demonstration walkthrough and presenter script.
- [**Production Deployment Guide**](docs/production-deployment.md) — Complete cloud deployment architecture on Render with Neon Serverless PostgreSQL.
- [**WhatsApp Setup & Integration Guide**](docs/whatsapp-setup.md) — Meta Cloud API configuration, template schemas, credential setup, and failure isolation.
- [**Recruiter / Client Evaluation Guide**](docs/project-evaluation-guide.md) — 5-to-10 minute code tour for technical reviewers.
- [**Technical Case Study & Incident Log**](docs/case-study.md) — Deep dive into 3 verified engineering incident resolutions and fault-injection drills.
- [**Deployment & Rehearsal Guide**](docs/deployment.md) — Service units, Caddy configuration, failure drills, and verification ledger.
- [**n8n Integration Guide**](docs/n8n-integration.md) — Complete workflow contract, node execution order, Telegram, and WhatsApp configuration.
- [**Lead Form Source**](demo/lead-form.html) — Client-facing responsive portal implementation.
