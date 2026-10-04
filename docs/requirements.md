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

## Out of scope

AI, n8n, CRM, authentication, Docker, background workers, unrelated features.
