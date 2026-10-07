# Lead Intelligence & Intake Automation — Requirements

This file is the source of truth for the project.

## Milestone 1: Lead Intake API

```
POST /leads
    ↓
validate request
    ↓
store lead in PostgreSQL
    ↓
return HTTP 201 with lead_id
```

## Input

| Field | Rules |
|---|---|
| `name` | required, non-empty string |
| `email` | required, valid email address (`pydantic.EmailStr`) |
| `company` | required, non-empty string |
| `message` | required, non-empty string |
| `source` | required, non-empty string |
| `external_id` | optional string (nullable) |

Validation is performed with Pydantic. Invalid or missing fields return HTTP 422.

## Database

PostgreSQL 16. Application user/database already exist:
- database: `lead_automation`
- user: `lead_app`

### Table `leads`

```sql
CREATE TABLE leads (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    company     TEXT NOT NULL,
    message     TEXT NOT NULL,
    source      TEXT NOT NULL,
    external_id TEXT NULL,
    status      TEXT NOT NULL DEFAULT 'new',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT leads_source_external_id_unique UNIQUE (source, external_id)
);
```

## API behavior

- `POST /leads` with valid input → **HTTP 201**, JSON body includes `lead_id`.
- `POST /leads` with invalid/missing fields → **HTTP 422**.
- `POST /leads` duplicating an existing `(source, external_id)` pair →
  **no second row is created**, and the API returns **HTTP 409 Conflict**.
- Uniqueness is enforced by a database `UNIQUE (source, external_id)` constraint.
  `NULL` `external_id` values do not collide with each other.
- Same `external_id` with a *different* `source` is allowed (uniqueness is on the pair).

## Engineering requirements

1. Use Pydantic for request validation.
2. Use parameterized SQL.
3. Read `DATABASE_URL` from environment configuration.
4. Never hardcode database credentials.
5. Keep database access separate from the API route.
6. Create a SQL schema/init file.
7. Add tests for: valid input, invalid email, missing fields, database
   persistence, and duplicate `external_id` behavior.
8. A duplicate `(source, external_id)` must not create a second lead.
9. Return HTTP 201 when creating a new lead.
10. Do not add AI, n8n, CRM, authentication, Docker, background workers,
    or unrelated features.

## Additional constraints

- `docs/requirements.md` is the source of truth for the project.
- Project/test configuration lives in `pyproject.toml` (no `pytest.ini`).
- PostgreSQL-specific exceptions stay inside `app/db.py`. The database/service
  layer translates a duplicate `(source, external_id)` into an
  application-level exception; the API route maps it to HTTP 409.
- Database tests must **fail loudly with a clear error** when the test
  PostgreSQL database is unavailable. No skipping.
- Email validation uses `pydantic.EmailStr`.
- Credentials are read only from the environment / `.env`.
  The PostgreSQL password must never be requested, printed, exposed,
  or hardcoded.
- The psycopg connection pool must be closed during FastAPI
  application shutdown (lifespan).
- Keep the implementation minimal.

## Milestone 2: Lead Retrieval

```
GET /leads?status=…&source=…&limit=…&offset=…   →  200 + items/total
GET /leads/{lead_id}                            →  200 | 404
```

### Endpoints

`GET /leads` — list leads, newest first (`created_at DESC, id DESC`).

| Query param | Rules |
|---|---|
| `status` | optional exact-match filter; unknown value → empty `200` |
| `source` | optional exact-match filter; unknown value → empty `200` |
| `limit` | optional, integer, default `20`, min `1`, max `100` |
| `offset` | optional, integer, default `0`, min `0` |

Response `200`:

```json
{"items": [LeadResponse, ...], "total": 123, "limit": 20, "offset": 0}
```

`GET /leads/{lead_id}`:

- `lead_id` path param must be an integer → otherwise **422**.
- Found → **200** with `LeadResponse` (same model as POST).
- Not found → **404** with a generic detail; no database error details exposed.

### Schema / migrations

No schema changes. Reuse table `leads` and its existing indexes/constraint.

### Engineering requirements (Milestone 2)

1. Parameterized SQL only; all SQL stays in `app/db.py`, none in routes.
2. Reuse the existing connection pool and lifespan; no new dependencies.
3. Read-only endpoints: `GET` requests must not mutate the database.
4. Route maps "not found" to 404; unexpected exceptions are not swallowed.
5. Tests use the same `TEST_DATABASE_URL` injection and fail loudly if it is
   missing; `TRUNCATE leads RESTART IDENTITY` before each test; no skips/xfails.

### Tests (Milestone 2, minimum)

1. No leads → `GET /leads` returns `200` with empty `items` and `total: 0`
2. Created lead appears in the list with correct fields
3. `status` filter narrows results; unknown `status` → empty `200`
4. `source` filter narrows results; unknown `source` → empty `200`
5. `limit`/`offset` paginate correctly; `total` unaffected by paging
6. Default ordering is newest first
7. `GET /leads/{id}` of an existing lead → `200` with its `lead_id`
8. `GET /leads/{id}` of a missing lead → `404`
9. Non-integer `lead_id` → `422`
10. `limit=0`, `limit=101`, `offset=-1` → `422`

### Out of scope for Milestone 2

AI, n8n, CRM, authentication, Docker, background workers, email,
frontend, write/update/delete endpoints (`PATCH`, `DELETE`), bulk export.

## Milestone 3: AI Lead Scoring

```
POST /leads → validate → persist → AI scoring (best effort) → 201 + score fields
```

Scoring is **synchronous and non-blocking for intake**: the lead is always
created and 201 returned even when AI is unavailable, slow, or misconfigured.
A duplicate still returns 409 **before** any scoring call (no wasted API call).

### Configuration (pydantic-settings, env / `.env`, never printed)

| Setting | Rules |
|---|---|
| `OPENAI_API_KEY` | optional; **absent → scoring skipped**, score stays `NULL` |
| `OPENAI_BASE_URL` | optional, defaults to the provider's public API |
| `LEAD_SCORING_MODEL` | optional, default `gpt-4o-mini` |

The key must never be hardcoded, printed, logged, or returned by any endpoint.

### Schema — `sql/002_add_lead_scoring.sql`

```sql
ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS score        INTEGER NULL,
    ADD COLUMN IF NOT EXISTS score_reason TEXT NULL,
    ADD COLUMN IF NOT EXISTS scored_at    TIMESTAMPTZ NULL;
ALTER TABLE leads
    ADD CONSTRAINT leads_score_range
    CHECK (score IS NULL OR (score >= 0 AND score <= 100));
```

Schema init and test setup apply all files in `sql/` in sorted order.

### Scoring behavior (isolated in `app/scoring.py`)

- Input to the model: `message`, `company`, `source` only (no name/email —
  minimize PII in prompts).
- Expected model reply: strict JSON `{"score": <0-100 int>, "reason": "<text>"}`,
  parsed with Pydantic. Out-of-range score, bad JSON, timeout (5 s), HTTP
  error, or missing key → scoring result is `None`.
- `None` → lead keeps `score = NULL`; API still returns **201**.
- Successful result is persisted with one parameterized `UPDATE` in `app/db.py`.
- Database errors still propagate (never swallowed); only AI-layer errors
  degrade to `NULL`.

### API

`LeadResponse` gains three nullable fields (POST, GET by id, and list):

| Field | Type |
|---|---|
| `score` | `int` or `null` (0–100) |
| `score_reason` | `str` or `null` |
| `scored_at` | `datetime` or `null` |

### Testing (offline — no API key, no network calls in tests)

The scorer is injected via a FastAPI dependency (`Depends`); tests override it
with a fake. Database tests keep the existing fail-loud `TEST_DATABASE_URL`
rules. Minimum scenarios:

1. No `OPENAI_API_KEY` → `POST /leads` → 201, `score: null` (graceful skip)
2. Fake scorer returns a result → 201 with `score`/`score_reason`/`scored_at`
3. Score is actually persisted (SELECT) and appears in GET/list responses
4. Fake scorer raises/timeout → 201, `score: null` (intake never fails)
5. Duplicate `(source, external_id)` → 409 and the scorer is **never called**
6. Parser unit tests: invalid JSON / out-of-range score → `None`
7. Existing M1 + M2 suites remain green unchanged

### Engineering requirements (Milestone 3)

1. Parameterized SQL only; all psycopg code stays in `app/db.py`.
2. AI/provider code stays in `app/scoring.py`; routes only orchestrate.
3. Prompts, model name, base URL come from configuration — no secrets in code.
4. `httpx` moves from the `test` extra to main dependencies (already installed;
   no new libraries added).
5. No background workers: single attempt, 5 s timeout, no retries.

### Out of scope for Milestone 3

n8n, CRM, authentication, Docker, background workers/queues, email,
frontend, streaming, token/billing accounting, `PATCH`/`DELETE`, bulk export.

## Milestone 4: Offline Heuristic Scoring

Motivation: no LLM API key is available, so Milestone 3's scoring is always
dormant. This milestone makes scoring produce real, deterministic scores
locally — no key, no network, no data leaves the machine.

### Behavior (supersedes M3's "no key → NULL" rule)

```
POST /leads → persist → scorer:
    OPENAI_API_KEY set    → LLM scorer (Milestone 3, unchanged)
    OPENAI_API_KEY absent → heuristic scorer (new, default)
    → 201 + score fields
```

- The route's injected scorer becomes this dispatcher; the LLM path keeps its
  5 s timeout and silent degradation (if the LLM fails while a key is set,
  the result stays NULL — it does **not** fall back to the heuristic).
- `score` is always an integer 0–100 with a non-empty human-readable
  `score_reason`; persisted through the existing `set_lead_score` UPDATE.
- No schema changes, no new settings, no new libraries.

### Heuristic rules (deterministic; exact weights live in `app/scoring.py`)

Inputs: `message`, `company`, `source`, and the **email domain only**
(local processing — unlike LLM prompts, nothing is sent anywhere, so the
domain signal is acceptable; local parts are never stored separately).

| Signal | Direction |
|---|---|
| Intent keywords in message (`demo`, `pricing`, `quote`, `trial`, `enterprise`, `integration`, `api`, …), case-insensitive | up per hit, capped |
| Substantial message (≥ 100 chars) | up |
| Trivial/short message (< 20 chars) | down |
| Corporate email domain (not gmail/outlook/yahoo/hotmail/…) | up |
| Free-mail domain | down |
| Source weight (`referral`, `contact-form` > `social`, `newsletter`) | ± |
| Base score | starting midpoint |

Result is clamped to 0–100; `score_reason` summarizes the top contributing
signals (e.g. `"High-intent keywords (demo, pricing); corporate email domain"`).

### Tests (offline, deterministic — replaces/extends M3 suite)

1. **Amends M3 test:** no key → `POST /leads` → 201 with heuristic
   `score` (0–100) and non-empty reason instead of `NULL`
2. High-intent message scores materially higher than junk message (same env)
3. Corporate email domain scores higher than free-mail domain (same message)
4. Identical payloads (different `external_id`) → identical score and reason
5. Heuristic score is persisted (SELECT) and visible in GET/list responses
6. Key present (stubbed `httpx.post`) → LLM scorer wins; stub not called when
   key absent
7. Every payload edge case stays within 0–100 with non-empty reason
8. Existing M1 + M2 suites green; M3 fake-scorer/409/parser tests unchanged

### Engineering requirements (Milestone 4)

1. Heuristic lives in `app/scoring.py`; routes stay orchestration-only.
2. Pure function of its inputs — no randomness, no clock, no I/O.
3. No secrets involved anywhere; nothing printed or logged.
4. Parameterized SQL only; psycopg stays in `app/db.py` (unchanged).
5. `score_reason` length bounded (≤ 280 chars) to keep responses tidy.

### Out of scope for Milestone 4

Configurable rule sets/weights, per-source tuning, ML models, prompt
engineering, LLM fallback chains, n8n, CRM, auth, Docker, workers, email,
frontend, `PATCH`/`DELETE`, bulk export.

## Milestone 5: Lead Status Workflow

```
PATCH /leads/{lead_id}  {"status": "contacted"}
    → 200 LeadResponse (updated) | 404 | 422
```

Completes the intake → retrieve → score → qualify loop with no external
dependencies (n8n/CRM remain out of scope; nothing new to install, no keys).

### Status vocabulary (closed set)

`new` → `contacted` → `qualified` → `closed`

- `status` is a required request field validated as a Pydantic `Literal` of
  the four values → unknown/missing/whitespace-only → **422**
- **No transition graph enforcement in M5**: any known status may be set from
  any current status (e.g. reopening a closed lead is allowed); same-status
  PATCH is an idempotent no-op → **200**
- Non-integer path id → **422**; unknown lead → **404** (generic detail)
- Existing rows (all `new`) are unaffected; POST keeps defaulting to `new`
- `GET /leads?status=` keeps Milestone 2 semantics (unknown value → empty 200)

### Schema — `sql/003_lead_status_workflow.sql`

```sql
ALTER TABLE leads DROP CONSTRAINT IF EXISTS leads_status_valid;
ALTER TABLE leads
    ADD CONSTRAINT leads_status_valid
    CHECK (status IN ('new', 'contacted', 'qualified', 'closed'));
```

Applied automatically by lifespan init and test setup (both already run every
file in `sql/` in sorted order). The database constraint is the authoritative
guard against invalid statuses, mirroring the Milestone 1 unique-constraint
pattern.

### Implementation shape

1. `app/models.py`: `LeadStatusUpdate(status: Literal["new", "contacted", "qualified", "closed"])`
2. `app/db.py`: `update_lead_status(lead_id, status) -> dict | None` — one
   parameterized `UPDATE ... RETURNING <lead columns>`; `None` → 404 in route
3. `app/routes/leads.py`: `PATCH /leads/{lead_id}` handler — validates body
   (FastAPI/Pydantic), calls db, maps `None` → 404, returns `LeadResponse`
   (score fields untouched by the update)
4. No changes to config, scoring, pyproject, .env.example, or M1–M4 code

### Tests (Milestone 5, minimum)

1. `new` → `contacted` → **200**, response shows `contacted`
2. Each vocabulary value accepted (parametrized: contacted, qualified, closed, new)
3. Unknown status value → **422**
4. Missing `status` field → **422**
5. Whitespace-only status → **422**
6. Non-integer `lead_id` → **422**
7. Unknown `lead_id` → **404**
8. Status actually persisted (direct `SELECT`)
9. Idempotent same-status PATCH → **200** twice
10. After update, `GET /leads?status=contacted` includes the lead and
    `?status=new` excludes it
11. Direct SQL with an invalid status is rejected by the DB CHECK
12. Existing M1–M4 suites remain green unchanged

### Out of scope for Milestone 5

Editing other lead fields (name/email/message), DELETE, transition-graph
enforcement, bulk/batch updates, audit/history of status changes, n8n, CRM,
auth, Docker, workers, email, frontend.

## Milestone 6: Lead Export & Deletion

Completes the CRUD lifecycle offline: full-fidelity CSV export for analysis
or CRM handoff, and single-lead deletion. No new libraries (stdlib `csv`),
no schema changes, no configuration.

### Export — `GET /leads/export?status=…&source=…`

- Response: **200**, `text/csv; charset=utf-8`,
  `Content-Disposition: attachment; filename="leads-export.csv"`
- Columns (full row — includes fields the JSON API intentionally omits):
  `id, name, email, company, message, source, external_id, status, score, score_reason, scored_at, created_at`
- Header row always present, even for zero results
- Same filter semantics as `GET /leads` (exact match; unknown value → CSV with
  header only); same ordering (`created_at DESC, id DESC`); **no pagination** —
  export is always the full filtered result set
- Proper CSV escaping via stdlib `csv` (commas, quotes, newlines in `message`
  must round-trip); timestamps ISO-8601
- **Route registration order matters:** `/leads/export` must be registered
  before `/leads/{lead_id}` (a path param would otherwise swallow `export`
  and return 422). No existing routes are changed.

### Deletion — `DELETE /leads/{lead_id}`

- **204** no content on success | **404** unknown id (generic detail) |
  **422** non-integer id
- Parameterized `DELETE … WHERE id = %s` in `app/db.py` (rowcount → bool)
- After deletion: `GET /leads/{id}` → 404, list total decreases, and the same
  `(source, external_id)` pair can be created again → 201
- Double delete → second request **404**

### Implementation shape

1. `app/db.py`: `export_leads(status, source) -> list[dict]` (all columns) and
   `delete_lead(lead_id) -> bool` — parameterized SQL only
2. `app/routes/leads.py`: `GET /leads/export` (builds CSV with stdlib
   `csv`/`io` in the route — presentation concern) and `DELETE /leads/{lead_id}`;
   export registered first
3. No changes to models, config, scoring, sql/, conftest, or pyproject

### Tests (Milestone 6, minimum)

1. Empty export → 200, CSV content-type, attachment filename, header-only body
2. Export contains a created lead with correct values per column (incl. score)
3. CSV escaping round-trip: `message` with comma, quotes, and newline parses
   back identically via `csv.reader`
4. `status`/`source` filters applied (unknown value → header only)
5. Export ordering is newest first
6. Delete existing lead → **204**; gone from `GET /leads/{id}` and from list
7. Delete unknown id → **404**; non-integer id → **422**
8. Delete twice → second **404**
9. After delete, same `(source, external_id)` recreates → **201**
10. Existing M1–M5 suites remain green unchanged

### Out of scope for Milestone 6

JSON export, Excel formats, pagination/streaming for huge exports, bulk
delete, soft delete/retention, auth on export/delete (still unauthenticated
like every endpoint), n8n, CRM, Docker, workers, email, frontend.

## Milestone 7: n8n Lead Intake Integration

The first automation boundary: one importable n8n workflow that accepts an
external lead over a webhook, forwards it to the existing `POST /leads`, and
returns FastAPI's **real** result to the caller. n8n orchestrates; FastAPI
keeps owning validation, persistence, duplicate detection, scoring and all
SQL. Full reference: [`docs/n8n-integration.md`](n8n-integration.md).

### Artifact — `n8n/lead-intake.json`

```
Lead Webhook (POST /webhook/lead-intake, responseMode: responseNode)
    ↓
Prepare Lead Payload      resolve the API base URL ($env or the documented
                          default) → apiLeadsUrl; map the six contract fields,
                          drop unknown fields, default source = "n8n-webhook"
                          — no validation rules
    ↓
Create Lead in FastAPI    HTTP Request POST {apiLeadsUrl}
                          JSON body · fullResponse · neverError ·
                          onError: continueRegularOutput · 10 s timeout · no retries
    ↓
Route on API Status       Switch on statusCode: 201 | 409 | 422 | fallback
    ↓
Respond Created (201) · Respond Duplicate (409) ·
Respond Validation Failed (422) · Respond Upstream Error (502)
```

### Webhook contract (envelope mirrors the API)

| `outcome` | Webhook code | Extra fields | Source |
|---|---|---|---|
| `created` | `201` | `lead` = FastAPI `LeadResponse` | API `201` |
| `duplicate` | `409` | `detail` | API `409` |
| `validation_failed` | `422` | `detail` = FastAPI field errors, passed through | API `422` |
| `upstream_error` | `502` | `detail` generic, `apiStatus` `null` when no response arrived | transport failure / unexpected status |

Every envelope carries `apiStatus` (what FastAPI actually returned). Success is
never fabricated: a webhook `201` exists only because PostgreSQL stored the row.

### Configuration

- `LEAD_API_BASE_URL` — base URL of the FastAPI service (no trailing `/leads`),
  read by the `Prepare Lead Payload` code node via n8n's `$env` accessor.
- **n8n 2.x blocks `$env` by default** (`N8N_BLOCK_ENV_ACCESS_IN_NODE` unset →
  denied), so the lookup is wrapped in `try/catch` and falls back to the
  `DEFAULT_API_BASE_URL` constant (`http://localhost:8000`) at the top of that
  node; set `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` to use the environment
  variable. The node writes `apiBaseUrlSource` (`environment` /
  `workflow-default …`) to every execution so the active source is visible.
- The shipped `"id": "lead-intake"` makes `n8n import:workflow` an upsert, and
  `n8n publish:workflow --id=lead-intake` activates it on n8n 2.x.
- No credentials in the JSON: the API is unauthenticated, so n8n needs no
  credential either. When auth is added later it attaches as an n8n credential.

### Tests / validation (Milestone 7)

1. Existing suites stay green (83 tests) — valid create, invalid payload,
   duplicate, and scoring-failure-never-breaks-intake are already covered by
   `tests/test_create_lead.py` and `tests/test_lead_scoring.py`; no redundant
   contract tests are added.
2. The artifact is parsed and structurally checked (every connection target
   exists, single root, no orphan nodes, no secrets) with a JSON parser, and
   accepted by n8n's own `validateWorkflowStructure` during
   `n8n import:workflow`.
3. **Live n8n execution was performed** against n8n 2.41.7 (import → publish →
   production webhook): created → `201`, duplicate → `409`, invalid payload →
   `422`, API stopped → `502` with `apiStatus: null`, upstream `500` → `502`
   with `apiStatus: 500`, and the env-blocked fallback → documented default.
   The scenario table lives in [`docs/n8n-integration.md`](n8n-integration.md).

### Out of scope for Milestone 7

Notifications beyond the records themselves (email/Slack — Telegram was
later added, narrowly, in Milestone 10), CRM handoff, enrichment, scheduled
follow-ups, retries/queues, authentication, rate limiting, Docker, multi-workflow
architecture, and any change to the FastAPI schema or endpoints.

## Milestone 8: n8n Lead Qualification Routing

The existing intake workflow becomes a basic qualification pipeline: FastAPI's
score (Milestones 3/4) is turned into a deterministic routing decision inside
n8n, with visibly divergent actions per branch. FastAPI keeps owning
validation, persistence, duplicate detection **and scoring**; n8n owns
orchestration, the qualification branch, and the routing actions. Full
reference: [`docs/n8n-integration.md`](n8n-integration.md).

### Rule (computed once, in one node)

```
score is not a finite number (null / missing)  →  manual_review  (priority null)
score >= threshold                             →  qualified      (priority high)
score <  threshold                             →  not_qualified  (priority normal)
```

- Threshold **70**, a configurable **initial business rule — not empirically
  validated** (no conversion data exists in this project). Grounding: the
  heuristic baseline is 45; observed representative scores cluster at
  12/15/35 (junk), 63/63 (corporate inbound, no intent keyword) and
  77–100 (explicit intent keyword) — 70 sits in the natural 63→77 gap.
- **Single configuration source:** the `DEFAULT_QUALIFIED_THRESHOLD` constant
  and the `LEAD_QUALIFIED_THRESHOLD` env probe live only in the
  `Prepare Lead Payload` config block (same block as the base URL); the
  `Qualification` node reads the resolved value from that node's output and
  never redefines it. Every execution records `qualifiedThreshold` +
  `thresholdSource`.
- `null` stays `null`: an unscored lead goes to `manual_review` with
  `score: null` in the record — never `0`, never silently not-qualified.
- The backend's persisted `status` field (`new/contacted/qualified/closed`,
  Milestone 5, manual PATCH) is **not** written by this workflow;
  `qualification` is a separate internal routing label.

### Artifact — added to `n8n/lead-intake.json`

```
Route on API Status ── 201 ──┬─► Respond Created            (Milestone 7 envelope, unchanged)
                             └─► Extract Lead + Score   normalize LeadResponse
                                      ↓
                                 Qualification        apply the rule once
                                      ↓
                                 Route on Qualification   switch on the label
                                      ├─ qualified     ─► Log Priority Route
                                      ├─ not_qualified ─► Log Normal Route
                                      ├─ manual_review ─► Log Manual Review
                                      └─ (fallback)    ─► Log Manual Review
(409 / 422 / fallback branches of Route on API Status are untouched)
```

**Notification (as built in M8):** this environment has no
email/Slack/CRM (Telegram delivery arrived later, in Milestone 10), so each
terminal node performs its action by
emitting one structured record (`action`, `leadId`, `company`, `source`,
`score`, `qualification`, `priority`, `qualifiedThreshold`, plus `note` on
manual review) **as its node output**, persisted in n8n's execution data —
workflow-native, inspectable, no credentials or third-party accounts.
*(Milestone 9 supersedes this shape: `action`/`note` became `event`/`reason`
plus a `summary` string and the unified audit event — see below.)*

### Tests / validation (Milestone 8)

1. Existing suites stay green (**83 tests**) — no backend change was needed
   (the `LeadResponse` contract already carries `lead_id`/`score`/`company`/
   `source`), so no new backend tests are added.
2. Structural validation of the workflow JSON (unique ids/names, connection
   integrity, reachability, single trigger, intended terminals only, M7 nodes
   and non-201 connections byte-identical to the previous commit, threshold
   defined in exactly one node, no secrets/paths, `typeVersion`s present in
   the installed `n8n-nodes-base`), plus n8n's own
   `validateWorkflowStructure` at import time.
3. **Live n8n execution, run personally** against n8n 2.41.7 (import →
   publish → production webhook), branch outcomes read from n8n's persisted
   execution data: qualified (95 → `Log Priority Route`), not-qualified
   (12 → `Log Normal Route`), unscored (`score: null` → `Log Manual
   Review`), duplicate → 409, invalid payload → 422, API stopped → 502 —
   with the 409/422/502 paths confirmed to **not** enter qualification —
   plus a threshold-flip run (`LEAD_QUALIFIED_THRESHOLD=95`, env allowed):
   the same score-75 lead that qualified at 70 became `not_qualified`
   (`thresholdSource: environment`), and score 95 at threshold 95 still
   qualifies (`>=`). Scenario table: [`docs/n8n-integration.md`](n8n-integration.md).

### Out of scope for Milestone 8

Delivering the routing records anywhere outside n8n's execution data
(email/Slack/CRM/webhook-out — none exists in this environment and none is
faked; Telegram was added later, in Milestone 10), enrichment, scheduled
follow-ups, retries/queues,
authentication, rate limiting, Docker, additional workflows, and **any change
to FastAPI** (no contract gap was found: `score` is already `int | None` and
the response already carries every field the routing needs).

## Milestone 9: Operational Lead-Alert & Audit Layer

The qualification decision becomes an operational record an operator can act
on and audit — without third-party credentials and without new
infrastructure. FastAPI keeps validation, persistence, duplicate detection
**and scoring**; n8n keeps orchestration and qualification and now also the
operational action layer. Full reference:
[`docs/n8n-integration.md`](n8n-integration.md).

### Design (entirely inside n8n's execution data)

```
Route on Qualification ──┬─ qualified     ─► Log Priority Route  ─┐
                         ├─ not_qualified ─► Log Normal Route     ─┤ operational
                         ├─ manual_review ─► Log Manual Review    ─┤ records
                         └─ (fallback)    ─► Log Manual Review    ─┘
                                                 ▼
                                          Build Audit Event   (terminal)
```

- **One structured record per branch, identical keys everywhere:** `event`
  (`lead_qualified` / `lead_not_qualified` / `lead_manual_review`), `leadId`,
  `company`, `source`, `score` (`null` stays `null` — never fabricated),
  `scoreReason`, `qualification`, `priority`, `reason`, `qualifiedThreshold`,
  `thresholdSource`, plus a human-readable **`summary` string on the same
  output JSON** — the n8n-native representation that survives into persisted
  execution data (static node "notes" cannot carry per-lead values and
  `console.log` is not persisted without `CODE_ENABLE_STDOUT=true`).
- **Unified audit event** in `Build Audit Event`: `timestamp`
  (`$now.toISO()`), `executionId`, `executionMode`, plus the branch record
  unchanged — one structured event per executed lead, deliberately shaped to
  feed a later consumer (Milestone 10 attached Telegram, but to the branch
  records; email/Slack/CRM/analytics remain future consumers). No database
  table, no FastAPI endpoint, no queue.
- **Caller-response isolation, measured rather than assumed:** n8n 2.x
  `executionOrder: v1` orders sibling nodes by canvas position (top-most
  runs first), which meant `Respond Created` actually ran *last* — so a
  failing operational node returned a fake `500` to the caller even though
  FastAPI had stored the lead (reproduced live, then fixed). Moving
  `Respond Created` above the first operational node makes the caller get
  the genuine `201` first; the failure stays visible as a `status: error`
  execution. Connection lists remain byte-identical to Milestone 7.
- Records are **workflow-native operational records**: Milestone 9 itself
  delivered nothing — no email/Slack/CRM integration exists in this
  environment and none is faked or claimed. (Milestone 10 below adds the
  first real delivery: Telegram, for `qualified` and `manual_review` only.)

### Tests / validation (Milestone 9)

1. Existing suites stay green (**83 tests**) — no backend change was needed
   (the `LeadResponse` contract already carries every field the records use).
2. Structural validation of the workflow JSON (15 unique nodes,
   reachability, only the intended terminals, every M7/M8 node and
   connection byte-identical to `HEAD` except the three rewritten branch
   records and `Respond Created`'s position, the respond-above-extract
   execution-order invariant, single config source, no SQL/scoring
   logic/secrets/paths, `typeVersion`s present in the installed sources).
3. **Live n8n execution, run personally** against n8n 2.41.7: qualified,
   not-qualified, unscored (bogus `OPENAI_API_KEY` + dead base URL),
   duplicate, validation failure, FastAPI down, threshold flip (the same
   score-75 lead qualified at 70 and became `not_qualified` at 95),
   boundary score, and a clean run — plus **three fault-injection runs**:
   pre-M9 ordering (caller received a fake `500` while the row was stored),
   early operational node with the fix (caller `201`, execution
   `status=error`, row accepted), and the new audit node failing (caller
   `201`, branch record still persisted, failure visible). Scenario table:
   [`docs/n8n-integration.md`](n8n-integration.md).

### Out of scope for Milestone 9

Any external delivery of the records (email/Slack/CRM — none exists in this
environment and none is faked or claimed; Telegram delivery arrived in
Milestone 10), persisting audit events
(no table, no endpoint, no queue — the event lives in n8n's execution data
on purpose), altering FastAPI (no contract gap: the webhook/lead contract
already carries every field the records need), plus everything excluded in
the preceding milestones.

## Milestone 10: Telegram Lead Notifications

The first real external business action of the workflow: the two
qualification outcomes a human must act on — `qualified` and
`manual_review` — are rendered from their existing operational records and
delivered as Telegram messages to one configured chat. FastAPI keeps
validation, persistence, duplicate detection **and scoring** (no backend
change was needed); n8n keeps orchestration, qualification and now the
notification. Full reference (policy, exact message samples, credential
setup, measured ordering, fault evidence):
[`docs/n8n-integration.md`](n8n-integration.md).

### Design (four appended nodes; nothing existing touched)

```
Log Priority Route ─► Build Qualified Notification ─► Send Qualified to Telegram
Log Manual Review  ─► Build Manual Review Notification ─► Send Manual Review to Telegram
```

- **Policy:** `qualified` → send; `manual_review` → send (a human must
  review); `not_qualified` → no send; `409` / `422` / `502` → no send
  (qualification, audit and notification never run).
- **Messages are renderings, not records:** built only from real record
  fields (`company`, `leadId`, `score`, `qualification`, `source`, plus
  `reason` for manual review) — nothing fabricated. The structured record
  stays authoritative: `score: null` remains `null` in the data while the
  message shows `Score: unavailable`. The build node's output is the record
  unchanged plus `chatId` and `telegramMessage`.
- **Destination & credential:** one chat, `TELEGRAM_CHAT_ID` on the n8n
  process (read via `$env`, explicit throw when missing or blocked); the bot
  token exists only in n8n's encrypted credential store (`telegramApi`), and
  the committed workflow carries only the non-secret `{id, name}` reference.
  No recipient lookup, no user mapping, no multi-tenant/dynamic channels, no
  other provider.
- **Audit before notification:** both build nodes sit below
  `Build Audit Event`, so n8n's v1 top-most-sibling-first ordering executes
  the audit first — verified from persisted start timestamps, in the success
  run *and* in the fault run.
- **Caller isolation preserved:** the Telegram nodes carry no `onError`
  override. A deliberately broken destination (invalid chat id, credential
  untouched) left the caller's genuine `201` intact, failed the execution
  with the Telegram error persisted (`Bad Request: chat not found`), and the
  audit event had already run before the failed send — verified live, not
  assumed from node placement.

### Tests / validation (Milestone 10)

1. Existing suites stay green (**83 tests**) — backend changes: **none**.
2. Structural validation of the workflow JSON (**19 nodes** = 15 M9 nodes
   byte-identical + 4 added; unique ids/names; every connection resolves; no
   orphans; the 7 intended terminals; Telegram `typeVersion` `1.2` present in
   the installed n8n sources; credential reference = `{id, name}` only;
   `not_qualified` cannot reach any Telegram node; audit positioned above both
   build nodes; hygiene regexes clean — no token, chat id, machine path, DSN
   or SQL).
3. **Live n8n execution, run personally** against n8n 2.41.7 — all seven
   cases: qualified lead (real Telegram delivery observed, `message_id: 9`),
   manual-review lead with `score: null` (real delivery observed,
   `message_id: 8`, `Score: unavailable`, structured score still `null`),
   not-qualified (0 Telegram node executions), duplicate → 409, validation →
   422, FastAPI down → 502 (all three: no Telegram), and the controlled
   Telegram fault (caller still `201`, execution `status=error` with the
   Telegram error visible, audit persisted before the send). Scenario table:
   [`docs/n8n-integration.md`](n8n-integration.md).

### Out of scope for Milestone 10

Any other channel or destination (email/Slack/CRM/webhook-out), recipient
lookup / user mapping / multi-tenant or dynamic channel selection, retries,
queues or a delivery pipeline for the send, delivery/read-receipt tracking
beyond the Bot API response persisted in the execution data, notification
rules or preferences, persisting audit events, altering FastAPI (no contract
gap was found), plus everything excluded in the preceding milestones.

## Milestone 11: Demo Lead Form

A single-file, framework-free contact form — `demo/lead-form.html` — that
acts as a real lead source for the existing automation: a prospective
customer fills it out and the submission travels the untouched path
**browser → n8n webhook → FastAPI → PostgreSQL + scoring → qualification →
audit → Telegram**. The form is a source, not a second backend: no scoring,
threshold or validation logic exists in the frontend, the backend contract
is unchanged (backend changes: none), and the workflow JSON was not touched
by this milestone. Full reference (run instructions, CORS evidence,
response-state mapping, verification table):
[`docs/n8n-integration.md`](n8n-integration.md).

### Design

- **Fields:** exactly the API contract — `name`, `email`, `company`,
  `message` — plus `source: website-contact-form` (the vocabulary the API
  tests already use), injected from one `CONFIG` block that also holds the
  single webhook URL (`http://localhost:5679/webhook/lead-intake` for this
  machine's n8n). No unsupported fields, no credentials, one configuration
  location, nothing scattered through the HTML.
- **Direct browser → n8n, no proxy:** n8n 2.41.7 answers the form's CORS
  preflight (`OPTIONS → 204` with reflected `Access-Control-Allow-*`) and
  returns `Access-Control-Allow-Origin` on the webhook response — tested
  against the running instance before implementation, so a plain
  cross-origin `fetch()` is used and neither a proxy nor an architecture
  change was needed.
- **Real response contract, not blanket success:** `201 created` → success
  panel ("Thanks. Your message has been received."); `409 duplicate` →
  duplicate state ("We already have this message."); `422
  validation_failed` → friendly per-field errors derived from field *names*
  only (raw server text never rendered); `502 upstream_error`, network
  failure or any unexpected response → a generic "We couldn't submit your
  request right now. Please try again." banner with the form's values
  retained. No scores, thresholds, execution IDs, workflow/database
  internals or stack traces are ever shown to the person submitting.
- **`external_id`:** browser-generated (`crypto.randomUUID()`), one id per
  message, kept across retries of that message and rotated after success —
  so an accidental retry cannot create a second row. Explicitly *not* a
  global-uniqueness claim; the server's `(source, external_id)` duplicate
  rule stays authoritative. `?external_id=<fixed-id>` pins the id for
  deliberate duplicate demonstrations — the milestone's only developer
  affordance, hidden in the URL and exposing nothing internal.
- **UX:** real business-form presentation (labels, required indicators,
  placeholders), inline validation with `aria-invalid` and focus
  management, disabled button + spinner while submitting, distinct
  success/validation/duplicate/upstream states, mobile-friendly layout
  (390 px viewport: no horizontal overflow, 16 px inputs, 50 px button).

### Tests / validation (Milestone 11)

1. Existing suites stay green (**83 tests**) — backend changes: **none**.
2. JS syntax (`node --check` on the extracted script) and HTML tag-balance
   checks; form fields audited against the FastAPI `LeadCreate` contract
   (no unsupported fields; the page contains no direct FastAPI call).
3. **Six scenarios driven through the real form in headless Chrome** (real
   value entry, real Submit click, requests observed in the browser's own
   network log — always `localhost:5679/webhook/lead-intake`, never
   FastAPI): qualified (Telegram delivery observed, `message_id: 11`),
   not-qualified (201, no send), manual review with `score: null` after
   restarting FastAPI unscored (Telegram delivery observed,
   `message_id: 12`), duplicate pinned id → `409` with exactly one row,
   invalid → client-side block (0 requests) plus an authoritative `422`
   (no row), FastAPI stopped → `502` (no row, no send). Every outcome read
   from n8n's persisted execution data and the database afterwards;
   scenario table: [`docs/n8n-integration.md`](n8n-integration.md).

### Out of scope for Milestone 11

User authentication, accounts, dashboards, lead lists, admin panels, a
production or hosted frontend, frontend frameworks/build tooling, webhook
authentication (none exists today), any backend or workflow change (none
was needed), plus everything excluded in the preceding milestones.

## Out of scope

n8n beyond the single lead-intake workflow of Milestones 7–11, CRM,
authentication, Docker, background workers, a production frontend or
hosting (the Milestone 11 form is a local demo artifact), unrelated
features.
(AI is in scope via the Milestone 3 spec above.)

## Milestone 14: Provider-Agnostic LLM Scoring with Heuristic Fallback

Milestone 14 supersedes the Milestone 4 behavior that returned no score when a
configured LLM failed. FastAPI continues to own provider selection, scoring,
score validation and fallback. n8n continues to own qualification, routing,
audit events and notification; the API and workflow contracts are unchanged.

### Configuration

Settings are centralized in `app/config.py` and loaded from environment or
`.env`. No API key is printed, logged or returned.

| Setting | Default | Behavior |
|---|---|---|
| `LLM_PROVIDER` | `openrouter` | `openrouter` or `openai-compatible` |
| `LLM_BASE_URL` | provider default | OpenRouter defaults to `https://openrouter.ai/api/v1`; the OpenAI-compatible option defaults to `https://api.openai.com/v1` |
| `LLM_MODEL` | `openrouter/free` | Model/router sent to the OpenAI-compatible chat completions endpoint |
| `LLM_API_KEY` | unset | Missing key skips the LLM and runs the heuristic |

The default route uses OpenRouter's `openrouter/free` model router through
`/chat/completions`; it can choose among currently available free models. The
model identifier is configuration, not business logic. The same HTTP
interface supports an OpenAI-compatible endpoint when configured with its
provider, endpoint, model and key. No OpenAI subscription or paid API credit
is required. The application does not configure a paid-provider fallback.

OpenRouter's current published limit for free-model requests is 50 requests
per day without purchased credits (checked 2026-10-07). Limits, model
availability, latency and output can change. A free API key/account is still
needed to call the provider; no claim of unlimited free inference is made.
References: [OpenRouter free router](https://openrouter.ai/docs/guides/routing/routers/free-router)
and [rate-limit FAQ](https://openrouter.ai/docs/faq#how-are-rate-limits-calculated).

### Scoring and fallback

```
configured LLM returns valid score → use LLM score
missing key / provider error / timeout / invalid output → existing heuristic
LLM and heuristic both fail → score = NULL
```

- The LLM receives only `source`, `company` and `message`; it does not receive
  lead name or email. Those three fields leave the server for the configured
  provider. The free-text message may itself contain personal or sensitive
  data; callers should not submit that content to a free third-party provider.
  Provider-specific data-use and retention terms apply; this path is for
  demo/portfolio use, not a promise of enterprise privacy or suitability for
  sensitive/regulated leads.
- Provider output must be a complete JSON object with a `score` that is an
  actual JSON integer from `0` through `100`, inclusive. Strings, booleans,
  floating-point values (including integral-looking floats), NaN/infinity,
  missing/duplicate score keys, prose, malformed JSON and out-of-range values
  are rejected. A missing or unusable reason does not invalidate a valid
  score; a generic reason is supplied.
- A failed or unusable LLM response falls back to the existing deterministic
  heuristic without changing its formula. If the heuristic also fails, intake
  still returns HTTP `201` with `score`, `score_reason` and `scored_at` null.
- No score provenance or provider diagnostics are added to the public API.
  The response fields and shapes remain unchanged. n8n still applies its
  existing `70` qualification threshold: integer scores route as before, and
  `score = null` routes to `manual_review`. No scoring or threshold logic is
  added to n8n.

### Tests and scope

Automated tests mock provider responses and remain offline. They cover LLM
success, provider error/timeout, malformed and boundary scores, heuristic
fallback, absent credentials and both scorers failing. No live provider calls
belong in the normal test suite. Provider/model quality and the `70` threshold
are not validated statistically; no accuracy or superiority claim is made.
