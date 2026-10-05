# Lead Intelligence Automation

A FastAPI + PostgreSQL backend that ingests B2B leads, scores them, and drives
them through a qualification workflow — built as the data backbone for an
upcoming **n8n automation layer**.

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
tests/               83 integration + unit tests
docs/requirements.md source of truth for project scope
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
(Milestones 1–6), plus a hardening pass (indexes, health probe, docs).

**Deliberately out of scope:** authentication, rate limiting, Docker, CRM,
background workers, frontend, email/Telegram, status transition-graph
enforcement. The service is not exposed publicly in its current form.

**Next:** the n8n automation layer — workflow triggers consuming this API for
lead routing, notifications and CRM handoff.
