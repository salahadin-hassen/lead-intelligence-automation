# Technical Recruiter & Client Evaluation Guide

**Target Evaluation Time:** 5 to 10 minutes
**Goal:** Quickly evaluate code quality, system design maturity, testing rigor, and engineering discipline in this repository without needing to spin up local infrastructure.

---

## Evaluation Flow at a Glance

| Step | Focus Area | Primary File to Inspect | Key Takeaway |
|---|---|---|---|
| **1** | **System Architecture** | [`README.md`](../README.md) & [`docs/case-study.md`](case-study.md) | Clear boundaries: FastAPI owns business logic, n8n orchestrates, DB enforces invariants. |
| **2** | **API Design & Validation** | [`app/routes/leads.py`](../app/routes/leads.py) | Pydantic schema validation, clean error mapping, and non-blocking scoring invocation. |
| **3** | **Database Layer & Integrity** | [`app/db.py`](../app/db.py) & [`sql/`](../sql/) | 100% parameterized SQL, zero driver leak to routes, connection pooling, DB constraints. |
| **4** | **AI Scoring & Fallback** | [`app/scoring.py`](../app/scoring.py) | Multi-tier scoring: LLM schema validation with automatic fallback to deterministic heuristic. |
| **5** | **Workflow Orchestration** | [`n8n/lead-intake.json`](../n8n/lead-intake.json) | Threshold-based qualification, caller-response isolation, execution-native audit records. |
| **6** | **Test Rigor & Philosophy** | [`tests/conftest.py`](../tests/conftest.py) | 97 automated tests; strict fail-loud design against real PostgreSQL instances. |
| **7** | **Operational Readiness** | [`deploy/bin/`](../deploy/bin/) & [`docs/deployment.md`](deployment.md) | Single-tenant deployment scripts, health probes, failure drills, and verified audit ledger. |

---

## Detailed 5-to-10 Minute Code Tour

### Step 1: Architecture Boundaries & Design Philosophy (1 Minute)
Start by reviewing the system design in [`README.md`](../README.md) and [`docs/case-study.md`](case-study.md):
- **Core Pattern:** FastAPI decides; n8n orchestrates.
- Notice that no database queries, business scoring logic, or schema validation rules are duplicated in n8n.
- Notice the explicit boundary between the client-facing presentation and backend ingestion.

### Step 2: Inbound Route & Error Mapping (2 Minutes)
Open [`app/routes/leads.py`](../app/routes/leads.py):
- **`create_lead` (Lines 84–122):** Demonstrates clean request processing.
- Notice how duplicate submissions are caught via an application-level `DuplicateLeadError` and cleanly translated into an HTTP `409 Conflict`.
- Notice how the scoring invocation is wrapped in a defensive `try/except` block: an AI inference failure logs a warning and degrades gracefully, but **never** aborts the creation of the lead row.

### Step 3: Database Isolation & Authoritative Constraints (1.5 Minutes)
Open [`app/db.py`](../app/db.py) and [`sql/001_create_leads.sql`](../sql/001_create_leads.sql):
- **SQL Isolation:** 100% of the repository's SQL queries are encapsulated inside `app/db.py`.
- **Parameterized Queries:** Every query uses parameterized bindings (`%s` or `%(named)s`), protecting against SQL injection.
- **Authoritative Constraints:** Inspect [`sql/001_create_leads.sql`](../sql/001_create_leads.sql) and [`sql/003_lead_status_workflow.sql`](../sql/003_lead_status_workflow.sql). Notice that uniqueness on `(source, external_id)` and allowed status values (`new`, `contacted`, `qualified`, `closed`) are enforced by PostgreSQL `CHECK` and `UNIQUE` constraints, not merely by application code.

### Step 4: Resilient AI Scoring with Heuristic Fallback (2 Minutes)
Open [`app/scoring.py`](../app/scoring.py):
- **PII Sanitization:** In `build_user_message` (Lines 34–36), observe that prospective customer `name` and `email` are stripped before calling third-party LLM providers.
- **Strict Output Validation:** In `parse_scoring_reply` (Lines 39–67), notice that LLM replies must be strict JSON containing an integer `score` within $0\text{--}100$. Floating-point numbers, booleans, missing keys, and duplicate JSON keys are rejected.
- **Deterministic Fallback:** In `score_lead_heuristic` (Lines 110–156), review the offline scoring engine. If external inference fails or times out ($>5\text{ s}$), scoring falls back immediately to a deterministic signal model (intent keywords, domain credibility, message length) with zero network calls.

### Step 5: n8n Workflow Orchestration & Caller Isolation (1.5 Minutes)
Open [`n8n/lead-intake.json`](../n8n/lead-intake.json) or read [`docs/n8n-integration.md`](n8n-integration.md):
- **Single Config Point:** Node `Prepare Lead Payload` manages API URLs and qualification threshold logic ($70$) in one location.
- **Caller-Response Isolation:** In the graph layout, notice that `Respond Created` sits above the operational logging and notification nodes. This design ensures that if the downstream Telegram Bot API is unavailable, the caller still receives an authentic HTTP 201 response.
- **Auditing:** Node `Build Audit Event` packages qualification metadata (`event`, `leadId`, `score`, `priority`, `timestamp`) into an immutable event stored in the execution record.

### Step 6: Testing Culture & Verification (1 Minute)
Open [`tests/conftest.py`](../tests/conftest.py) and scan [`tests/`](../tests/):
- **Fail-Loud Design:** Review lines 22–30 of `tests/conftest.py`. The suite deliberately prohibits skips (`pytest.skip`) or xfails. If the PostgreSQL test database is unreachable, the suite fails immediately with an explicit error.
- **Coverage:** 97 automated tests cover payload validation, database persistence, CSV export escaping, status lifecycle updates, LLM timeouts, and heuristic edge cases.

---

## Key Questions Technical Evaluators Often Ask

**Q: Why not build the entire automation inside n8n without FastAPI?**
**A:** No-code tools lack first-class support for software testing, database connection pooling, schema migrations, and parameterized queries. By separating concerns, our core business logic and persistence remain under version control and 97 automated tests, while n8n is leveraged where it shines: event orchestration and messaging.

**Q: How does the system prevent lost leads during traffic spikes or network outages?**
**A:** If third-party AI scoring is slow or unavailable, intake immediately falls back to the deterministic local heuristic without delaying the response. In addition, the client-side form mints an `external_id` per message: if a client experiences a network glitch and retries, the database unique constraint prevents duplicate rows.

**Q: Is this system currently deployed to a cloud cluster?**
**A:** The codebase is fully single-tenant deployment-ready (with systemd service units, Caddy proxy configurations, and automated health probes in `deploy/`), but is not hosted on a permanent public domain. It has been verified through a production-shaped local rehearsal stack and temporary authenticated Cloudflare Quick Tunnels.
