# Lead Intelligence Automation

A FastAPI + PostgreSQL backend that ingests B2B leads, scores them, and drives
them through a qualification workflow — built as the data backbone for an
**n8n automation layer** (`n8n/lead-intake.json`).

This is not a CRUD demo: intake is designed to survive an unavailable scoring
backend, uniqueness and data integrity are enforced by the database rather than
application code, every query is parameterized, and the test suite runs against
a real PostgreSQL instance and fails loudly rather than skipping when one is
missing.

## Capabilities

- **Lead intake** — `POST /leads` with Pydantic validation, duplicate detection
  on `(source, external_id)` → `409`, never a second row.
- **Retrieval** — paginated, filterable listing (`status`, `source`) plus
  single-lead lookup, newest first.
- **Lead scoring** — deterministic offline heuristic by default, optional
  OpenAI-compatible LLM scoring when a key is configured. Scoring failures
  never fail intake: the lead is still created with `score = NULL`.
- **Status workflow** — `new → contacted → qualified → closed`, validated in
  Pydantic *and* guarded by a database `CHECK` constraint.
- **CSV export & deletion** — full-fidelity CSV (including fields the JSON API
  omits) with proper escaping; single-lead `DELETE`.
- **Health probe** — `/health` returns `200` only when PostgreSQL is actually
  reachable, `503` otherwise.
- **n8n intake workflow** — importable webhook → `POST /leads` → response
  workflow that forwards FastAPI's real status instead of fabricating success,
  routes created leads (qualified / not-qualified / manual review) by the
  score FastAPI returned, and records each decision as a structured
  operational event with a human-readable summary in the execution data.

## Architecture

```
route (orchestration only)
  ├── app/db.py       all SQL + psycopg3 connection pool
  └── app/scoring.py  all AI/provider I/O (LLM + heuristic)
        ↓
   PostgreSQL 16
```

| Path | Responsibility |
|---|---|
| `app/main.py` | App wiring, lifespan (pool → schema → shutdown), `/health` |
| `app/routes/leads.py` | HTTP endpoints, error mapping, CSV assembly |
| `app/db.py` | Every SQL statement, pool lifecycle, `DuplicateLeadError` translation |
| `app/scoring.py` | LLM call, offline heuristic, dispatcher |
| `app/models.py` | Pydantic request/response models |
| `app/config.py` | Settings from environment / `.env` |
| `sql/` | Idempotent migrations, auto-applied at startup |
| `n8n/lead-intake.json` | Importable n8n workflow — orchestration, qualification routing and operational/audit records only; validation, scoring and SQL stay in FastAPI |

Rules the code enforces: SQL never leaves `app/db.py`, provider code never
leaves `app/scoring.py`, and PostgreSQL-specific errors never reach a route.

## Quickstart

**Prerequisites:** Python 3.12+, PostgreSQL 16 with a database and role
(defaults assume `lead_automation` / `lead_app`).

```bash
# 1. Install (editable, with test extras)
python3.12 -m venv .venv
.venv/bin/pip install -e ".[test]"

# 2. Configure — never commit real credentials
cp .env.example .env
#    then edit DATABASE_URL / TEST_DATABASE_URL

# 3. Run (migrations in sql/ are applied automatically on startup)
.venv/bin/uvicorn app.main:app --reload

# 4. Verify
curl localhost:8000/health          # {"status":"ok"}
open http://localhost:8000/docs     # interactive API docs
```

No migration step to remember: every file in `sql/` runs in sorted order on
each boot and is idempotent by construction.

## Testing

```bash
.venv/bin/python -m pytest
```

**83 tests**, fully offline with respect to AI providers (scorers are injected
through FastAPI dependencies; no API key, no network calls to any LLM).

Tests run against a **real PostgreSQL test database** named by
`TEST_DATABASE_URL`. By design there are no skips and no xfails: if the test
database is unreachable or the variable is unset, the suite fails immediately
with an explicit error. The schema is rebuilt before the session and the
`leads` table is truncated before every test.

## API reference

| Method | Path | Success | Errors |
|---|---|---|---|
| `POST` | `/leads` | `201` | `422` validation · `409` duplicate |
| `GET` | `/leads` | `200` `{items, total, limit, offset}` | `422` bad pagination |
| `GET` | `/leads/{id}` | `200` | `404` · `422` |
| `PATCH` | `/leads/{id}` | `200` | `404` · `422` |
| `DELETE` | `/leads/{id}` | `204` | `404` · `422` |
| `GET` | `/leads/export` | `200` `text/csv` attachment | `422` |
| `GET` | `/health` | `200` database reachable | `503` unreachable |

### Create a lead

```bash
curl -X POST localhost:8000/leads \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Ada Lovelace",
    "email": "ada@example.com",
    "company": "Analytical Engines Ltd",
    "message": "Please schedule a demo of the enterprise plan.",
    "source": "website-contact-form",
    "external_id": "ext-1001"
  }'
```

`name`, `email`, `company`, `message` and `source` are required and must be
non-empty (`email` validated as `EmailStr`); `external_id` is optional. The
response carries `lead_id`, `status` (defaults to `new`) and the score fields.

### List and filter

| Query param | Rules |
|---|---|
| `status` | exact match; unknown value → empty `200` |
| `source` | exact match; unknown value → empty `200` |
| `limit` | default `20`, `1–100` |
| `offset` | default `0` |

```bash
curl 'localhost:8000/leads?status=new&source=website-contact-form&limit=20'
```

Ordering is always `created_at DESC, id DESC` (newest first), backed by the
indexes in `sql/004_add_lead_indexes.sql`.

### Export and delete

```bash
curl 'localhost:8000/leads/export?status=contacted' -o leads.csv  # full row set
curl -X DELETE localhost:8000/leads/123                           # 204
```

Export always returns the complete filtered result set (no pagination) with a
header row even when empty; `message` commas/quotes/newlines round-trip safely.

### Update status

```bash
curl -X PATCH localhost:8000/leads/123 \
  -H 'Content-Type: application/json' \
  -d '{"status": "contacted"}'
```

Allowed values: `new`, `contacted`, `qualified`, `closed`. The vocabulary is a
closed set — any other value (or a missing one) is `422`. Transition-graph
enforcement is intentionally out of scope: any status may be set from any
status, and same-status updates are idempotent.

## Lead scoring

The route's scorer is a dispatcher:

```
OPENAI_API_KEY set    → LLM scorer (OpenAI-compatible /chat/completions,
                        5 s timeout, temperature 0, strict JSON reply)
OPENAI_API_KEY absent → deterministic offline heuristic (default)
```

- **Heuristic** — pure function of the lead: base score, high-intent keyword
  hits in `message` (capped), message length, corporate vs. free-mail domain,
  and source weight. Clamped to `0–100` with a human-readable `score_reason`
  (≤ 280 chars). No randomness, no clock, no I/O, no data leaves the machine.
- **LLM** — prompt input is limited to `source`, `company` and `message`
  (no name or email, to minimize PII). Replies are parsed with Pydantic;
  bad JSON, out-of-range scores, timeouts and HTTP errors all degrade to
  `score = NULL`.
- **Intake never fails because of scoring.** A scorer exception is logged and
  the lead is still created; database errors, by contrast, always propagate.
  A duplicate `(source, external_id)` returns `409` *before* any scoring call.

## n8n integration

`n8n/lead-intake.json` is the automation boundary: an importable workflow that
receives an external lead over a webhook, hands it to this API, and routes the
created lead by its score.

```
Webhook (POST /webhook/lead-intake)
  → Prepare Lead Payload   single config block (API base URL + qualification
                           threshold, env var or documented default), map the
                           six contract fields, drop unknown ones
  → HTTP Request           POST {base}/leads (JSON, 10 s, no retries)
  → Switch on statusCode   201 | 409 | 422 | fallback
      ├ 201 ─┬ Respond Created (201) ────────────► caller, envelope unchanged
      │       │   (executes first, so operational failures can't fake a 5xx)
      │       └ Extract → Qualification → Route on Qualification
      │            ├ qualified     → Log Priority Route  (priority "high")
      │            ├ not_qualified → Log Normal Route    (priority "normal")
      │            └ manual_review → Log Manual Review   (score stays null)
      │                        └──► Build Audit Event    unified event
      │       M10 notification chains (below the audit node → audit runs first):
      │            qualified     → Build Qualified Notification     → Telegram send
      │            manual_review → Build Manual Review Notification → Telegram send
      │            (not_qualified · 409 · 422 · 502 → no Telegram)
      ├ 409 → Respond Duplicate · 422 → Respond Validation Failed
      └ other → Respond Upstream Error (502)
```

- **n8n orchestrates, FastAPI decides.** No validation rules, scoring logic or
  SQL are duplicated in the workflow; the HTTP node uses `neverError` +
  `onError: continueRegularOutput` so a 409/422/5xx flows through the graph
  instead of failing the execution. The score is only *compared* (one Code
  node, threshold read from the config block) — never recomputed.
- **Qualification rule:** `score >= threshold` → `qualified` (high priority),
  `score < threshold` → `not_qualified` (normal), `score` not a number
  (FastAPI could not score) → `manual_review`, reported as `null` — never
  turned into `0`. The threshold **70** is a configurable *initial business
  rule, not empirically validated* (see [`docs/n8n-integration.md`](docs/n8n-integration.md)
  for the score-distribution reasoning); override it with
  `LEAD_QUALIFIED_THRESHOLD`.
- **No fabricated success.** Every webhook answer is an envelope
  `{"outcome", "apiStatus", ...}` whose code mirrors what FastAPI returned
  (`created`/201, `duplicate`/409, `validation_failed`/422, `upstream_error`/502
  when the API could not be reached, `apiStatus: null` when no response arrived
  at all).
- **Operational records first, real notifications second (M10).** Each
  branch emits its record (`event`:
  `lead_qualified`/`lead_not_qualified`/`lead_manual_review`, lead data,
  `reason`, threshold provenance) plus a human-readable `summary`, and
  `Build Audit Event` merges it into one audit event (`timestamp`,
  `executionId`, `executionMode`) — persisted and inspectable in the
  execution view. Since Milestone 10, the two actionable outcomes
  (`qualified`, `manual_review`) additionally **deliver a real Telegram
  message** to one configured chat, rendered from that very record —
  nothing fabricated, structured `score: null` stays `null` while the
  message reads `Score: unavailable`. `not_qualified`, 409, 422 and 502
  send nothing; the bot token lives only in n8n's encrypted credential
  store — no secret in the JSON, and no channel other than this single
  Telegram destination exists.
- **Intake answers never depend on the operational chain.** n8n 2.x orders
  sibling nodes by canvas position (top-most first), so `Respond Created`
  sits above the first operational node and replies before qualification,
  audit or notification code runs; a failing operational node shows up as a
  `status: error` execution while the caller keeps its real `201` (verified
  by fault injection — including the M10 Telegram-fault run: caller still
  `201`, execution `status=error` with `chat not found` persisted, audit
  event already written before the failed send).
- **Configuration:** `LEAD_API_BASE_URL` + `LEAD_QUALIFIED_THRESHOLD` in the
  n8n process environment — note that **n8n 2.x blocks `$env` by default**, so
  also set `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` (otherwise the workflow falls
  back to the `DEFAULT_*` constants at the top of the `Prepare Lead Payload`
  node, and its output shows which source was used). No secrets are stored in
  the JSON — the API is unauthenticated today.

```bash
n8n import:workflow --input=n8n/lead-intake.json   # upserts by the shipped id
n8n publish:workflow --id=lead-intake              # activate (n8n 2.x)
# restart n8n if it was already running, then:
curl -X POST localhost:5678/webhook/lead-intake -H 'Content-Type: application/json' \
  -d '{"name":"Ada Lovelace","email":"ada@example.com","company":"Analytical Engines Ltd",
       "message":"Please send pricing for the enterprise plan.",
       "source":"partner-referral","external_id":"ext-9001"}'
```

**Validated against a live n8n 2.41.7 instance** — intake codes
(created/duplicate/invalid/unreachable/upstream-500), all three
qualification branches (qualified / not-qualified / unscored→manual review,
plus a `LEAD_QUALIFIED_THRESHOLD` flip run), the operational records and
audit event, three failure-isolation runs (operational node throwing →
caller still gets `201`, execution records the error, lead row accepted),
and all seven Milestone 10 notification scenarios — **actual Telegram
deliveries** for qualified (`message_id: 9`) and manual review
(`message_id: 8`), no send for not-qualified/409/422/502, and a controlled
Telegram fault (genuine `201` + `status=error` + audit-before-send) —
executed live with outcomes read from n8n's persisted execution data, plus
`pytest` (83 passing). Details and the exact scenario tables:
[`docs/n8n-integration.md`](docs/n8n-integration.md).

## Configuration

Read from the environment or `.env` (`.env` is gitignored; `.env.example` is
the committed template). Secrets are never printed, logged or returned.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | yes | — | Application PostgreSQL DSN |
| `TEST_DATABASE_URL` | tests only | — | DSN the test suite runs against |
| `OPENAI_API_KEY` | no | *(unset → heuristic)* | Enables LLM scoring |
| `OPENAI_BASE_URL` | no | `https://api.openai.com/v1` | Provider endpoint |
| `LEAD_SCORING_MODEL` | no | `gpt-4o-mini` | Scoring model |

The variables below are **n8n-side** (the environment of the n8n process),
not part of this service's `.env`:

| Variable (n8n process env) | Required | Default | Purpose |
|---|---|---|---|
| `LEAD_API_BASE_URL` | production | `http://localhost:8000` (constant in the workflow) | FastAPI base URL the workflow calls |
| `LEAD_QUALIFIED_THRESHOLD` | no | `70` (constant in the workflow) | Score ≥ threshold ⇒ `qualified` (initial rule, see [`docs/n8n-integration.md`](docs/n8n-integration.md)) |
| `TELEGRAM_CHAT_ID` | for notifications | — (a destination must be configured) | Single chat both Telegram sends go to; unset or `$env`-blocked fails the build node loudly (failed execution, caller unaffected) |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE` | no | *(unset = blocked on n8n 2.x)* | `false` lets the workflow read all of the variables above |

## Data model

Table `leads` (see `sql/001`–`004`):

```
id BIGSERIAL PK · name · email · company · message · source
external_id (nullable) · status DEFAULT 'new' · created_at
score (0–100) · score_reason · scored_at
```

- `UNIQUE (source, external_id)` — `NULL` values never collide; the same
  `external_id` under a different `source` is allowed.
- `CHECK (score BETWEEN 0 AND 100)` — enforced by PostgreSQL, not just Pydantic.
- `CHECK (status IN ('new','contacted','qualified','closed'))`.
- Indexes: `(created_at DESC, id DESC)` for the ordered list/export path and
  `(status, created_at DESC, id DESC)` for status-filtered views — chosen from
  measured query plans; `source` needs no index because the unique constraint
  already provides one.

## Project structure

```
app/
  main.py            FastAPI app, lifespan, /health
  routes/leads.py    endpoints (orchestration + CSV building)
  db.py              all SQL, connection pool
  scoring.py         LLM + heuristic scoring
  models.py          Pydantic schemas
  config.py          settings
  errors.py          application-level exceptions
sql/                 001–004 idempotent migrations
n8n/lead-intake.json importable webhook → POST /leads → routing + audit records
tests/               83 integration + unit tests
docs/requirements.md source of truth for project scope
docs/n8n-integration.md  the n8n contract and operating guide
```

## Design principles

- **The database is authoritative** — constraints, not just application code,
  reject duplicate leads, invalid statuses and out-of-range scores.
- **Layered by failure domain** — AI-layer errors degrade to `NULL`; database
  errors propagate; intake never fails because scoring is unavailable.
- **Fail loudly** — tests never skip; a missing test database is an error with
  a clear message, not a green build.
- **Minimal surface** — parameterized SQL only, no secrets in code, no
  framework beyond FastAPI + psycopg.

## Scope and roadmap

**In scope today:** intake, retrieval, scoring, status workflow, export,
deletion, health — as specified in [`docs/requirements.md`](docs/requirements.md)
(Milestones 1–6), plus a hardening pass (indexes, health probe, docs), the
n8n intake workflow (Milestone 7), its qualification routing (Milestone 8),
the operational/audit layer (Milestone 9) and Telegram notifications for the
two actionable outcomes (Milestone 10).

**Deliberately out of scope:** authentication, rate limiting, Docker, CRM,
background workers, frontend, email/Slack and any notification channel or
destination beyond the single configured Telegram chat, status
transition-graph enforcement, retries/queues, multi-workflow automation. The
service is not exposed publicly in its current form.

**Next:** more n8n workflows on top of this boundary — additional
notification channels/rules, lead routing and CRM handoff.
