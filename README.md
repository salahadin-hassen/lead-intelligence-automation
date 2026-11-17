# Lead Intelligence Automation

> An automated inbound lead qualification and routing system built with FastAPI, PostgreSQL and n8n: deterministic intent scoring (optional LLM scoring), database-enforced deduplication, and Telegram operator alerts (WhatsApp alerts implemented, live delivery pending Meta credentials).

**Public Deployment:** [https://leadintel.sahelyon.com](https://leadintel.sahelyon.com)  
**Interactive Lead Demo:** [https://leadintel.sahelyon.com/demo](https://leadintel.sahelyon.com/demo)  
**Production Release Status:** Last recorded production verification was on commit [`2fc400f`](https://github.com/salahadin-hassen/lead-intelligence-automation/commit/2fc400f) (14 deployment gates passed). A read-only check on 2026-11-16 found `/` and `/demo` returning HTTP 200, serving a page byte-identical to `demo/lead-form.html` at commit `9109f81` (including the AI-insights panel). The exact API commit, the live n8n workflow and the Render AI configuration have **not** been established, and no end-to-end production submission was made. See [Verification Status](#verification-status).

---

## 30-Second Project Overview (For Clients & Recruiters)

| Question | Detail |
| :--- | :--- |
| **What does it do?** | Ingests B2B contact inquiries, rejects duplicate submissions at the database level, scores buying intent ($0\text{--}100$) with a deterministic heuristic (optionally an LLM), and alerts sales teams via Telegram. |
| **Public Demo** | Deployed at [`https://leadintel.sahelyon.com`](https://leadintel.sahelyon.com) (Render free tier; cold starts possible). Dedicated `/demo` route with form validation and duplicate replay testing. |
| **Production Architecture** | Cloudflare $\rightarrow$ Caddy 2.8 Edge Ingress $\rightarrow$ n8n 2.41 Webhook $\rightarrow$ Authenticated FastAPI (Python 3.12) $\rightarrow$ Neon Serverless PostgreSQL 16. |
| **Verified Guarantees** | Zero duplicate leads (database unique constraints), deterministic heuristic scoring with optional LLM scoring, caller-response isolation, and **308 automated tests** (0 skips, 0 xfails; run 2026-11-16 against an isolated local PostgreSQL test database). |
| **What is Implemented?** | Website lead intake (production-verified at `2fc400f`), pilot Gmail lead intake workflow (`LeadIntel_Leads` label boundary; locally tested, not production-verified), database deduplication, heuristic intent scoring, Telegram sales alerts, WhatsApp Cloud API alert routing (live delivery unverified), and optional AI lead insights (off by default; the panel is visible on the public demo, but the production AI integration is unverified). *(CRM sync and multi-tenant OAuth platform are planned for future phases).* |

---

## Portfolio Summary (Quick Read)

Inbound B2B lead generation often stalls on manual triage: validating contact details, checking for duplicate inquiries across campaigns, scoring purchase intent, and manually alerting sales reps. **Lead Intelligence Automation** is an automated pipeline engineered to solve this end-to-end.

Incoming inquiries from a responsive, client-facing web portal are filtered through access-controlled ingress, ingested by an **n8n** automation workflow, and persisted in **PostgreSQL 16** via a **FastAPI** service. Inbound messages are scored by a local, deterministic heuristic scorer by default; when `LLM_API_KEY` is configured, a provider-agnostic LLM scorer (OpenRouter free router or OpenAI-compatible) is tried first and falls back to the heuristic if inference is unavailable or malformed. High-priority leads (`score >= 70`) trigger Telegram notifications to sales (WhatsApp Business Cloud alerts are wired with the same routing but require Meta credentials for live delivery), while all leads receive structured, execution-native audit logging.

Engineered with defensive failure isolation: upstream intake never fails due to scoring or notification outages. Covered by **308 automated tests** (fail-loud test database requirement), rehearsal failure drills, and workflow-level notification error boundaries.

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
           ├── Optional (only if LLM_API_KEY is set): OpenRouter / OpenAI-compatible LLM
           └── Default / fallback: Deterministic local heuristic scorer
    │
    ▼ Response (201 Created with score & reason)
n8n Qualification & Audit Layer
    ├── score >= 70  ──► Qualified (Priority: High)   ──► Telegram Alert (+ WhatsApp, pending Meta credentials)
    ├── score <  70  ──► Not Qualified (Priority: Normal) (Operational audit log only)
    └── score is null ──► Manual Review (Unscored)     ──► Telegram Review Alert (+ WhatsApp, pending Meta credentials)
    │
    └──► Build Unified Audit Event (Persisted in n8n execution data)
```

> **Gmail Integration Reference:** Complete OAuth setup instructions, Google Cloud consent screen configuration, dedicated label filtering (`LeadIntel_Leads`), normalization rules, and Render Free idle sleeping limitations are documented in [`docs/gmail-lead-ingestion.md`](docs/gmail-lead-ingestion.md).

---

## Engineering Highlights

- **Separation of Concerns:** FastAPI owns data integrity, SQL queries, schema validation, and scoring. n8n owns workflow orchestration, routing decisions, audit event assembly, and multi-channel notification dispatch. Scoring logic is never duplicated in n8n.
- **Database-Enforced Invariants:** PostgreSQL `CHECK` constraints enforce score bounds (`0 <= score <= 100`) and status vocabulary (`new`, `contacted`, `qualified`, `closed`). A database `UNIQUE (source, external_id)` constraint prevents race-condition duplicates.
- **Defensive Multi-Tier Scoring:** When enabled, LLM scoring parses strict JSON schemas. If the provider times out ($>5\text{ s}$), errors, or returns non-integer scores, the system seamlessly falls back to an offline heuristic analyzer. If both fail, `score` degrades cleanly to `NULL` without blocking lead intake.
- **Caller-Response Isolation:** In n8n, the HTTP `201 Created` response executes *before* the downstream qualification, audit, Telegram, and WhatsApp nodes. A downstream outage (such as Telegram or Meta WhatsApp API downtime) never delays or causes a false 500 error for the prospect.
- **Dual-Channel Mobile Alerts with Isolated Error Boundaries:** Priority (`qualified`) and `manual_review` leads trigger Telegram messages and WhatsApp Business Cloud API alerts (Telegram delivery verified in a local rehearsal; WhatsApp live delivery not yet verified). Both WhatsApp send nodes configure `onError: continueErrorOutput` to prevent notification failures from terminating sibling branches or executions.
- **PII Protection & Data Minimization:** Prompts sent to external LLMs contain only `source`, `company`, and `message` (customer `name` and `email` are never transmitted). WhatsApp notifications send only contact coordinates and omit free-text messages to minimize external exposure.
- **Responsive B2B Web Experience:** The client-facing demo portal is built in self-contained, dependency-free HTML5/CSS/JS with accessible form states, client-side validation, live payload preview telemetry, and dynamic `external_id` generation for duplicate prevention.
- **Strict Testing Philosophy:** 308 automated tests with zero skips and zero xfails. Tests require a real PostgreSQL database instance and fail loudly with descriptive errors if it is missing.

---

## Reliability Evidence

The system's reliability guarantees have been verified across automated unit/integration tests, local rehearsal-stack failure drills, and temporary public-demo browser sessions:

| Scenario | Expected Behavior | Evidence Level | Verification Detail |
|---|---|:---:|---|
| **Valid Lead Ingestion** | Lead validated, persisted in PostgreSQL, returns HTTP 201 with ID and score | **Automated & Public** | `pytest` integration test + public browser test (Lead ID 5, score 100) |
| **Duplicate External ID** | Database unique constraint triggers HTTP 409 Conflict; no duplicate row | **Automated & Public** | `pytest` + public submission with reused ID (`Respond Duplicate` panel) |
| **Invalid Payload** | Pydantic validation fails; returns HTTP 422 with field-level errors | **Automated & Public** | `pytest` + public form submit with invalid fields (HTTP 422) |
| **FastAPI Unavailable** | n8n catches connection failure and returns HTTP 502 Upstream Error | **Local Drill** | Rehearsal drill with stopped API process; caller received 502 with clean JSON |
| **LLM Provider Outage** | When LLM scoring is enabled, automatically falls back to deterministic local heuristic scorer; lead created | **Automated & Local** | `pytest` (mocked timeouts & HTTP 429/503) + rehearsal run with bad credentials |
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

To maintain engineering credibility with clients and technical recruiters, the repository explicitly separates what is **implemented** from **future roadmap items**. Verification level for each implemented item is listed in [Verification Status](#verification-status).

| Capability Area | Implemented MVP | Planned / Future Roadmap |
| :--- | :--- | :--- |
| **Inbound Capture** | Dedicated web portal (`/`) and interactive demo route (`/demo`) with client-side submission key generation (`external_id`); pilot Gmail label-based intake workflow. | Ingestion from external forms (Typeform, Calendly, LinkedIn ads). |
| **Edge Ingress** | Caddy 2.8 reverse proxy handling TLS and same-origin `/webhook/lead-intake` routing. | Custom enterprise vanity domains with automated SSL rotation. |
| **Data Validation** | FastAPI + Pydantic schema validation (422 Unprocessable Entity on malformed payloads). | Dynamic client-defined schema validation via UI builder. |
| **Data Deduplication** | Database-enforced `UNIQUE (source, external_id)` constraint returning HTTP 409 Conflict. | Fuzzy cross-campaign prospect matching and merge rules. |
| **Intent Scoring** | Deterministic heuristic scorer (default); optional OpenRouter / OpenAI-compatible LLM scorer with heuristic fallback. | Fine-tuned domain models and adaptive ML qualification thresholds. |
| **Caller Isolation** | HTTP 201 response delivers before downstream notification nodes run. | Asynchronous background broker (Celery / RabbitMQ) for batch ingestion. |
| **Sales Notifications** | Telegram Bot API alerts + WhatsApp Cloud API template routing with error boundaries. | Bidirectional CRM synchronization (HubSpot, Salesforce, Pipedrive). |
| **AI Lead Insights** | Optional OpenAI summary / intent / urgency / next action per lead, budget-capped, never changes the score ([docs](docs/ai-lead-insights.md)). | — |
| **Deployment** | Dockerized deployment on Render Web Services + Neon Serverless PostgreSQL 16, fronted by Cloudflare. | Multi-tenant SaaS architecture with user login & RBAC dashboard. |

---

## Verification Status

Status as of 2026-11-16. Column meanings:

- **Locally verified:** covered by the automated suite run that day against an isolated local PostgreSQL test database. External services (LLM providers, Telegram, Meta, Gmail, OpenAI) are mocked or exercised through workflow-JSON / Node.js evaluation, never called live.
- **Publicly observed:** seen through read-only GET requests to the public site and services that day. No lead was submitted and no webhook was called, so this proves presence, not behaviour.
- **Production end-to-end verified:** a recorded production run of the full flow.

| Capability | Implemented | Locally verified | Publicly observed (2026-11-16) | Production end-to-end verified |
| :--- | :---: | :---: | :--- | :--- |
| Website intake, validation (422), dedup (409) | Yes | Yes | `/` and `/demo` return HTTP 200; page matches `demo/lead-form.html` at `9109f81` | Yes, at commit `2fc400f`; not re-run since |
| Deterministic heuristic scoring | Yes | Yes | — | Yes, at `2fc400f`. Expected default because `render.yaml` sets no `LLM_API_KEY`; live environment not inspected |
| OpenRouter / OpenAI-compatible LLM scoring | Yes | Yes (mocked provider) | — | **No.** Only the provider-failure fallback was rehearsed locally; no recorded successful live LLM scoring run |
| Telegram alerts | Yes | Yes (workflow routing) | — | **No.** Live delivery verified in a local rehearsal only (execution 135) |
| WhatsApp alerts | Yes | Yes (workflow routing) | — | **No.** Pending Meta Developer account, credentials and approved templates ([setup](docs/whatsapp-setup.md)) |
| Gmail intake (pilot) | Yes | Yes (normalizer + workflow JSON) | — | **No.** Requires manual Google OAuth credential binding; polling pauses while Render sleeps ([guide](docs/gmail-lead-ingestion.md)) |
| AI lead insights | Yes (off by default) | Yes (mocked OpenAI) | The AI-insights panel is on the public `/demo` page. The public API schema includes `GET`/`POST /leads/{lead_id}/insights` (consistent with commit `97656c7` or later) | **No.** The live n8n `lead-intake` workflow has not been inspected, Render AI settings (`AI_INSIGHTS_ENABLED`, `OPENAI_API_KEY`) have not been verified, and no successful end-to-end delivery is recorded. A visible panel does not mean the integration works ([rollout steps](docs/ai-lead-insights.md)) |

---

## Known Scope & Operational Boundaries

- **Qualification Threshold:** The qualification threshold ($70$) is an initial operational business rule based on observed heuristic score clustering; it is not a statistically validated machine learning conversion model.
- **Production Deployment:** `leadintel.sahelyon.com` is served through Cloudflare. Responses come from the Render `leadintel-caddy` service (header `x-render-origin-server: Caddy`), which serves the page itself and proxies `/webhook/*` to `leadintel-n8n.onrender.com`. n8n calls `leadintel-api-2y0i.onrender.com`, backed by Neon Serverless PostgreSQL. A Cloudflare Worker that proxies to Render and pings Caddy every 10 minutes (06:00–24:00 EAT) was described in earlier versions of this README. That Worker is **not defined in this repository** and its behaviour has not been verified. Because Caddy serves the page itself, pinging Caddy does **not** wake the API or n8n.
- **Cold Starts (observed 2026-11-16):** Render free-tier services sleep after 15 minutes without traffic, and each wakes independently.
  - A cold API `/health` request took ~23.3 s.
  - A cold n8n `/healthz` request timed out after 150 s. A retry answered after ~96.9 s, about 4 minutes after the first request.
  - The demo form aborts requests after 120 s, so a submission that has to wake n8n can fail in the browser.
  - Warm requests historically took ~4–5 s.
  - Before a demo, check API and n8n readiness separately (see the [client demo guide](docs/client-demo-guide.md#pre-call-readiness-check-cold-start-mitigation)).
- **Stale Worker Build Risk:** the Render Caddy image copies `demo/lead-form.html` at build time, so it serves the current source. The repository's Cloudflare Worker config (`wrangler.json`) works differently: it uploads whatever is in `var/pages-public/` as static assets and serves them for `/`. That directory is gitignored and generated by `deploy/bin/build-pages.sh`. The local copy, generated 2026-10-20, matches `demo/lead-form.html` at `3f7dc1f` and has **no** AI-insights panel. Running `npx wrangler deploy` without first re-running `bash deploy/bin/build-pages.sh` would replace the current public frontend with that older page.
- **Live WhatsApp Delivery:** Workflow routing, template definitions, and isolated error boundaries are fully implemented in `n8n/lead-intake.json`. Live message delivery requires an active Meta Developer account, verified sender/recipient phone numbers, and approved message templates (see [`docs/whatsapp-setup.md`](docs/whatsapp-setup.md)).
- **Access Control Scope:** Ingress protection via Caddy; FastAPI implements fail-closed Bearer token authentication (`API_AUTH_TOKEN`) in production (`APP_ENV=production`).
- **Free LLM Provider Constraints:** LLM scoring only runs when `LLM_API_KEY` is set. OpenRouter free router availability depends on third-party daily quotas (50 requests/day). If exhausted or unavailable, the deterministic heuristic scorer takes over automatically.

---

## Quickstart & Local Verification

### Prerequisites
- Python 3.12+
- PostgreSQL 16 with a role and **two** databases, e.g. `lead_app` / `lead_automation` (app) and `lead_automation_test` (tests only)
- Node.js 18+ on `PATH` (verified with Node.js 24). 91 of the 308 tests run workflow and Worker JavaScript through `node`; no `npm install` is needed for the tests.

### 1. Installation
```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[test]"   # the test extra installs pytest and PyYAML
```

### 2. Configuration
```bash
cp .env.example .env
# Edit DATABASE_URL and TEST_DATABASE_URL in .env
```

> **Warning:** `TEST_DATABASE_URL` must point at a dedicated, disposable local database. The test session drops and recreates the `leads` and `ai_insights_budget` tables in it. Never point it at a production or shared staging database. Tests only use `TEST_DATABASE_URL`; they never connect to `DATABASE_URL`.

### 3. Run Automated Tests
```bash
.venv/bin/python -m pytest -q
# Expected result: 308 passed (verified 2026-11-16 in a fresh virtual environment)
```

Tests fail loudly (no skips) if the test database is unreachable or `node` is missing.

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
- [**AI Lead Insights**](docs/ai-lead-insights.md) — Optional OpenAI summary, intent, urgency and next action per lead; off by default, never changes the deterministic score.
- [**n8n Integration Guide**](docs/n8n-integration.md) — Complete workflow contract, node execution order, Telegram, and WhatsApp configuration.
- [**Lead Form Source**](demo/lead-form.html) — Client-facing responsive portal implementation.
