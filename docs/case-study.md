# Case Study: Engineering Reliable Lead Ingestion and Automated Qualification

## 1. Project Overview

**Lead Intelligence Automation** is an automated inbound lead ingestion, qualification, and routing system. It captures B2B prospective customer inquiries through a web contact form, enforces schema and duplicate validation, persists records in PostgreSQL 16, scores purchase intent using a hybrid LLM and heuristic engine, and dispatches real-time sales alerts via Telegram.

The system was engineered as a portfolio-grade reference architecture for businesses looking to automate lead intake without sacrificing software reliability, data integrity, or observability.

---

## 2. Problem and Objective

In typical sales and marketing operations, inbound inquiries from web forms are processed through ad-hoc automations or manual inbox triage. This introduces operational friction:
- **Intake Latency:** High-intent inquiries often wait hours or days for human triage before being assigned to an account executive.
- **Data Duplication:** Form resubmissions and page refreshes create duplicate records across downstream tools, fragmenting customer context.
- **Inconsistent Triage:** Manual evaluation of lead quality is subjective and variable, leading to missed opportunities or wasted sales capacity on low-intent inquiries.
- **Silent Failures:** Standard webhook-to-spreadsheet or no-code automations fail silently when upstream APIs return unexpected payloads or experience transient errors, resulting in lost leads.

**Engineering Objective:** Build a resilient, testable, and observable pipeline that guarantees:
1. Leads are durably persisted with database-enforced integrity.
2. Uniqueness is guaranteed at the database level.
3. External dependency failures (AI inference, messaging APIs) never cause data loss or false errors for the prospect.
4. Downstream operators have immediate visibility into qualification outcomes and execution failures.

*Note: This project was developed as a simulated enterprise B2B intake system using synthetic benchmark scenarios.*

---

## 3. Key Architectural Decisions

### Decision 1: Separation of Authority — FastAPI Decides, n8n Orchestrates
- **Rationale:** No-code/low-code workflow engines like n8n excel at event orchestration, webhook triggers, and third-party integrations, but become unmaintainable and fragile when loaded with domain business logic, SQL queries, and complex validation rules.
- **Implementation:** FastAPI owns all schema validation (Pydantic), PostgreSQL transactions, duplicate conflict detection, and scoring algorithms. n8n acts strictly as an orchestration pipe: it forwards webhooks to FastAPI, compares the returned numeric score against a configured threshold, and routes the lead accordingly.
- **Trade-off:** Requires maintaining a dedicated API service alongside n8n rather than building the entire pipeline inside n8n nodes, but ensures testability, version-controlled business logic, and strict encapsulation.

### Decision 2: Database as the Authoritative Invariant Enforcer
- **Rationale:** Application-level validation is insufficient for concurrency and data integrity in high-throughput environments.
- **Implementation:** Data integrity is backed directly by PostgreSQL 16 constraints:
  - `CONSTRAINT leads_source_external_id_unique UNIQUE (source, external_id)`: Rejects duplicate submissions at the storage engine level, returning HTTP 409 Conflict.
  - `CHECK (score IS NULL OR (score >= 0 AND score <= 100))`: Enforces score boundaries at the database layer.
  - `CHECK (status IN ('new', 'contacted', 'qualified', 'closed'))`: Prevents invalid lifecycle status values from ever being written.
- **Trade-off:** Schema migrations require explicit SQL scripts (`sql/001`–`004`), but guarantee zero corrupt or duplicate records regardless of application bugs or race conditions.

### Decision 3: Multi-Tier Scoring with Deterministic Heuristic Fallback
- **Rationale:** Relying solely on external LLM inference introduces critical failure modes: network latency, provider rate limits, API outages, and non-deterministic formatting errors.
- **Implementation:** The scoring subsystem uses an automatic fallback hierarchy:
  1. Primary: Configurable OpenAI-compatible endpoint (defaults to OpenRouter's `openrouter/free` router).
  2. Validation: Strict JSON parser requiring an integer `score` within `0–100`.
  3. Fallback: If the LLM call times out ($>5\text{ s}$), returns an HTTP error, or outputs malformed data, control transfers automatically to a local, deterministic heuristic scorer based on intent keywords, email domain credibility, and message length.
  4. Terminal: If both scoring mechanisms fail, the lead is stored with `score = NULL`, returning HTTP 201 to the client and routing to `manual_review` in n8n.
- **Trade-off:** Local heuristic scoring requires maintaining a ruleset in code, but guarantees that intake never halts due to third-party AI downtime or quota exhaustion.

### Decision 4: Caller-Response Isolation
- **Rationale:** A lead submission should be confirmed to the customer as soon as it is safely recorded. Operational failures in downstream notifications (e.g., Telegram API rate limits) must not return a fake HTTP 500 error to a prospect whose lead was successfully saved.
- **Implementation:** In n8n's execution graph, `Respond Created` is executed immediately upon receiving FastAPI's 201 response. Downstream qualification, audit event creation, and Telegram notifications execute in parallel/subsequent branches.
- **Trade-off:** Requires careful management of n8n's canvas layout and node execution order, but guarantees the prospect always receives an honest `201 Created` status code.

---

## 4. Engineering Incidents Discovered and Resolved

### Incident 1: False HTTP 500 Errors Caused by n8n Execution Ordering
- **Observed Symptom:** During fault-injection drills, deliberately failing downstream audit or notification nodes caused the webhook caller to receive an HTTP 500 Internal Server Error, even though FastAPI had successfully committed the lead to PostgreSQL.
- **Diagnosis:** In n8n 2.x with `executionOrder: v1`, parallel branches originating from a single node are evaluated sequentially based on their vertical canvas coordinate (`position[1]`), executing the top-most sibling first. Because `Respond Created` was visually positioned below the operational nodes, n8n attempted to execute the operational chain before answering the webhook. When an operational node threw an error, n8n aborted execution and returned a generic 500.
- **Fix:** Vertically repositioned `Respond Created` above the `Extract Lead + Score` and audit nodes on the canvas (`position: [720, -120]`).
- **Verification:** Fault-injection test with an intentional runtime exception in the operational branch confirmed that the caller received an authentic HTTP 201 response with the lead envelope, while the execution was logged as `status: error` in n8n.
- **Remaining Caveat:** Any manual repositioning of nodes in the n8n UI canvas can inadvertently change execution priority; canvas positions must be treated as execution code.

### Incident 2: Asynchronous Workflow Publication Lag Serving Stale Logic
- **Observed Symptom:** After executing `n8n import:workflow` and `n8n publish:workflow`, incoming webhooks continued executing the previous version of the workflow for several seconds, leading to inconsistent test results during automated deployment rehearsals.
- **Diagnosis:** In n8n 2.41.7, workflow publications are processed asynchronously through an internal outbox queue (`workflow_publication_outbox`). Running instances first serve the cached active version until a background reconciliation loop processes the outbox and updates the active version pointer.
- **Fix:** Enforced offline workflow imports via `deploy/bin/import-workflow.sh` while n8n is stopped. Enhanced `deploy/bin/stack.sh start` with an explicit convergence probe that queries `workflow_entity.activeVersionId` and verifies the publication outbox is drained before declaring the service ready.
- **Verification:** Verified across cold-start and restart drills; `stack.sh` reliably blocks until the imported workflow is confirmed active.
- **Remaining Caveat:** Operators must avoid editing or publishing workflows directly in the running web editor in production environments.

### Incident 3: Cloudflare Quick Tunnel Email Gate Causing Ingestion 401s
- **Observed Symptom:** During public demo exposure using `cloudflared tunnel` with `--allowed-mail`, the contact form loaded successfully, but subsequent same-origin `POST /webhook/lead-intake` requests failed with HTTP 401 Unauthorized from Cloudflare.
- **Diagnosis:** Cloudflare's browser-based email PIN authentication attaches session cookies that were not properly included or evaluated during same-origin AJAX/fetch POST requests in the cloudflared zero-trust proxy (an upstream issue documented in `cloudflared#1752`).
- **Fix:** Removed the experimental Cloudflare email PIN gate and relocated access control to the local edge proxy. Configured Caddy with HTTP Basic Authentication (`deploy/caddy/Caddyfile.local`) using Argon2id password hashes (`deploy/bin/create-demo-auth.sh`), protecting both the static form and the webhook endpoint uniformly before proxying.
- **Verification:** Public browser tests confirmed unauthenticated GET and POST requests returned HTTP 401, while authenticated requests loaded the form (HTTP 200) and submitted leads (HTTP 201) without preflight or cookie issues.
- **Remaining Caveat:** Quick Tunnels are transient by nature and rotate hostnames upon process restart; suitable for short evaluation sessions, not permanent production hosting.

---

## 5. Verification Summary

The codebase has been verified through a multi-layered testing strategy:

```
[ Automated Tests (97) ] ──► [ Rehearsal Drills (29) ] ──► [ Public Demo (End-to-End) ]
 (FastAPI + PG + Scorer)       (Full Stack + Failures)       (Authenticated Browser)
```

1. **Automated Test Suite (97 Tests Passing):**
   - **Lead Intake & Validation (14 tests):** Valid payload persistence, email validation, missing/blank fields, duplicate external ID rejection (`(source, external_id)`).
   - **Retrieval & Pagination (12 tests):** Status and source filtering, offset/limit validation, ordering by `created_at DESC, id DESC`, 404 handling.
   - **Lead Status Workflow (12 tests):** Vocabulary validation (`new`, `contacted`, `qualified`, `closed`), idempotent updates, database CHECK constraint verification.
   - **CSV Export & Deletion (10 tests):** Unpaginated CSV generation, character escaping, single-lead deletion, duplicate re-creation after deletion.
   - **Scoring Engine & Fallback (43 tests):** Strict JSON parsing, score boundary enforcement ($0\text{--}100$), heuristic signal weighting, LLM timeout and HTTP error fallback, provider selection, and null score propagation.
   - **Health Probe & DB Indexes (6 tests):** `/health` reporting 200/503 based on database connectivity, verification of composite indexes matching query plans.
   - *Test Philosophy:* Zero skips, zero xfails. Tests execute against a dedicated PostgreSQL instance and fail loudly if the database is unreachable.

2. **Local Rehearsal Stack & Smoke Tests:**
   - Executed against a complete production-shaped environment: FastAPI (`:8010`), PostgreSQL 16 (`:5499`), n8n (`:5678`), and Caddy (`:8080`).
   - `deploy/bin/smoke.sh` verified 11 assertions end-to-end, including lead creation, duplicate rejection, and audit generation.
   - 29 fault-injection assertions verified system resilience: FastAPI process down ($\rightarrow$ 502), PostgreSQL down ($\rightarrow$ 503 health probe), and invalid Telegram credentials ($\rightarrow$ isolated error execution with intact 201 response).

3. **Public Demo Verification (Cloudflare Tunnel + Caddy Auth):**
   - Verified live in an authenticated browser session on 2026-10-12:
     - Unauthenticated requests rejected with HTTP 401.
     - Synthetic qualified lead submitted $\rightarrow$ HTTP 201 returned to browser, lead persisted in DB, n8n routed to `Log Priority Route`, Telegram delivery confirmed with `ok: true`, `message_id: 25`.
     - Synthetic unqualified lead submitted $\rightarrow$ HTTP 201 returned to browser with score 27, n8n routed to `Log Normal Route`, zero Telegram notifications dispatched.
     - Resubmission of duplicate payload $\rightarrow$ HTTP 409 Conflict returned to browser and duplicate warning panel displayed.

---

## 6. Business Value Delivered (Qualitative)

For an organization deploying this pipeline, the system delivers tangible operational improvements:
- **Instant Triage for High-Value Leads:** High-intent inquiries trigger instant sales notifications, reducing response time from hours to seconds.
- **Zero Duplicate Contamination:** Eliminates redundant inbound records before they enter operational workflows or downstream CRMs.
- **Consistent Qualification Standards:** Evaluates every inbound prospect against objective intent markers and AI analysis, eliminating human bias.
- **Auditability and Compliance:** Every submission produces an immutable, structured operational record with execution metadata, scoring rationale, and timestamp.
- **Operational Resilience:** Sales and intake pipelines do not break when external third-party APIs experience transient downtime.

---

## 7. Engineering Trade-offs and Lessons Learned

1. **Pragmatic Orchestration vs. Pure Code:**
   Building workflows entirely in custom code increases maintenance overhead for notification templates and routing rules. Conversely, building business logic in no-code tools creates unmaintainable, untestable spaghetti. Placing data authority in a robust API and orchestration in n8n proved to be the optimal middle ground.
2. **Defensive Design Must Be Measured, Not Assumed:**
   Architectural assumptions (e.g., "parallel branches execute independently") must be verified through rigorous failure drills. The discovery of n8n's canvas-ordering behavior underscored the necessity of testing negative and failure states as thoroughly as happy paths.
3. **No Skips Testing Culture:**
   Enforcing that automated tests fail loudly when external test databases are offline prevented silent CI pass-throughs and guaranteed that all 97 tests reflect genuine data store interactions.
