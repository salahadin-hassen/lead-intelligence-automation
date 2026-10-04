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

AI, n8n, CRM, authentication, Docker, background workers, email, Telegram,
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

n8n, CRM, authentication, Docker, background workers/queues, email, Telegram,
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
Telegram, frontend, `PATCH`/`DELETE`, bulk export.

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
auth, Docker, workers, email, Telegram, frontend.

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
like every endpoint), n8n, CRM, Docker, workers, email, Telegram, frontend.

## Out of scope

n8n, CRM, authentication, Docker, background workers, unrelated features.
(AI is in scope via the Milestone 3 spec above.)
