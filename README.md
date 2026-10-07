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
- **Lead scoring** — provider-configurable OpenRouter free-model routing by
  default, with a deterministic local heuristic fallback. OpenAI-compatible
  providers are optional; scoring failures never fail intake.
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
- **Demo lead form** — a single-file, framework-free contact form
  (`demo/lead-form.html`) acting as a real lead source: it submits through
  the n8n webhook like any other caller and maps the workflow's actual
  response envelope to success / validation / duplicate / upstream states.
- **Deployable as a single-client instance** — systemd units, a Caddy
  reverse proxy (public HTTPS → same-origin form + `/webhook/*`), externalized
  env files with placeholder-only templates, and reproducible
  start/stop/logs/migrations/healthcheck/smoke commands. Validated on a
  production-shaped local stack including failure drills; **deployment-ready,
  not publicly deployed** (see [`docs/deployment.md`](docs/deployment.md)).

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

Automated tests run fully offline with respect to AI providers (provider
responses are mocked; no API key or network calls to any LLM).

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

The route's scorer is a provider-configurable dispatcher:

```
LLM_API_KEY set       → configured LLM via /chat/completions
LLM_API_KEY absent or LLM fails → deterministic heuristic
both scorers fail     → score = NULL (manual review in n8n)
```

- **Default provider** — OpenRouter at `https://openrouter.ai/api/v1`, using
  the configurable `openrouter/free` model router. The router may select
  among available free models; availability, rate limits and output can vary.
  OpenRouter currently documents 50 free-model requests per day without
  purchased credits; this provider limit can change. The application does not
  fall back to a paid model or provider. See the
  [free router docs](https://openrouter.ai/docs/guides/routing/routers/free-router)
  and [current rate-limit FAQ](https://openrouter.ai/docs/faq#how-are-rate-limits-calculated).
  OpenAI-compatible providers can be configured with `LLM_PROVIDER`,
  `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY`. No OpenAI subscription or
  paid API credits are required by this application.
- **Heuristic** — pure function of the lead: base score, high-intent keyword
  hits in `message` (capped), message length, corporate vs. free-mail domain,
  and source weight. Clamped to `0–100` with a human-readable `score_reason`
  (≤ 280 chars). No randomness, no clock, no I/O, no data leaves the machine.
- **LLM** — prompt input is limited to `source`, `company` and `message`
  (no name or email, to minimize PII). Those fields are sent to the configured
  provider. The message may itself contain personal or sensitive data, so
  don't send such leads through free third-party inference. Provider-specific
  data-use and retention terms apply; this free path is intended for
  demo/portfolio use, not sensitive or regulated leads. The score must be a
  JSON integer in `0–100`; malformed responses trigger heuristic fallback.
- **Intake never fails because of scoring.** The response contract is
  unchanged. A `null` score means both the LLM and heuristic scorer were
  unavailable. Database errors still propagate. A duplicate
  `(source, external_id)` returns `409` *before* any scoring call.

## n8n integration

`n8n/lead-intake.json` is the automation boundary: an importable workflow that
receives an external lead over a webhook, hands it to this API, and routes the
created lead by its score.

```
Demo lead form (demo/lead-form.html in a browser)
  ── fetch() POST JSON ──────────────────────────────────────────────►
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
# simplest: the shipped script does both steps offline (n8n stopped)
deploy/bin/import-workflow.sh && deploy/bin/stack.sh restart
# equivalent by hand:
n8n import:workflow --input=n8n/lead-intake.json   # upserts by the shipped id
n8n publish:workflow --id=lead-intake              # activate (n8n 2.x)
# restart n8n if it was already running, then:
curl -X POST localhost:5678/webhook/lead-intake -H 'Content-Type: application/json' \
  -d '{"name":"Ada Lovelace","email":"ada@example.com","company":"Analytical Engines Ltd",
       "message":"Please send pricing for the enterprise plan.",
       "source":"partner-referral","external_id":"ext-9001"}'
```

Note for operators: n8n applies publications asynchronously — after an
import+publish the *running* instance can briefly serve the previous
version. Restarting after the import (as the script tells you to) and
waiting for convergence is mandatory; `stack.sh start` does that wait
automatically (see [`docs/deployment.md`](docs/deployment.md)).

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

## Demo lead form

`demo/lead-form.html` is a single-file, framework-free contact form — the
human lead source for the workflow above. It exists to make the pipeline
demoable end to end, not as a product frontend.

```bash
# prerequisites: PostgreSQL, FastAPI on :8000, n8n on :5679 with the
# workflow published (same services as the n8n section above)
python3 -m http.server 8001 --directory demo
# open http://localhost:8001/lead-form.html
```

- **One configuration point:** `demo/config.js` (`window.LEAD_FORM_CONFIG`)
  — `webhookUrl`, default `http://localhost:5679/webhook/lead-intake` (this
  machine's n8n; change it there if n8n lives elsewhere). A deployment never
  edits files by hand: `deploy/bin/render-form.sh` regenerates the served
  `config.js` from `form.env`'s `DEMO_N8N_WEBHOOK_URL`. If the file or URL is
  missing, the form shows a visible setup error and refuses to submit. The
  `source` label (`website-contact-form`, the vocabulary the API tests use)
  is a constant next to the form logic.
- **Request path:** browser → n8n `POST /webhook/lead-intake` → FastAPI
  `POST /leads` → PostgreSQL + scoring → qualification → audit → Telegram.
  The form never calls FastAPI directly, contains no scoring or qualification
  logic, and exposes no scores or internals to the person filling it in.
- **CORS: only the local cross-origin case needs it.** In local development
  the form (`localhost:8001`) posts cross-origin to n8n (`localhost:5679`):
  n8n 2.41.7 answers the preflight (`OPTIONS` → `204`) and the workflow's
  webhook option allowlists exactly that origin
  (`allowedOrigins: http://localhost:8001` — the only workflow change in the
  deployment milestone; other origins get a mismatched
  `Access-Control-Allow-Origin` and are blocked by the browser). In
  production the form is served from the same domain as `/webhook/*`
  (Caddy), so requests are same-origin: no preflight, no CORS dependence —
  verified in the browser (zero `OPTIONS` requests observed).
- **States come from the workflow's real envelope:** `201 created` → success
  panel ("Thanks. Your message has been received."), `409 duplicate` → "we
  already have this message", `422 validation_failed` → friendly per-field
  errors (server field *names* only — no raw server text), `502
  upstream_error` or network failure → "We couldn't submit your request right
  now. Please try again." Nothing about scores, thresholds, execution IDs or
  stack traces is ever shown.
- **`external_id`:** generated in the browser per message
  (`crypto.randomUUID()`, reused across retries of that message, rotated after
  success) so an accidental double-submit cannot create a second row — demo
  convenience, explicitly *not* a global-uniqueness guarantee. For duplicate
  demos open `lead-form.html?external_id=<fixed-id>` and submit twice: the
  second attempt renders the duplicate state, while the server keeps owning
  the `(source, external_id)` rule.
- **Local demo by default, rendered for deployment.** Development serves it
  from a throwaway `python3 -m http.server`; a deployment serves the rendered
  copy under `var/www` through Caddy (same origin as the webhook). No build
  step, no dependencies, no credentials, no auth either way — it is a lead
  source for this pipeline, not a product frontend, and it is **not publicly
  deployed today**.

Personally verified end to end from a real browser (headless Chrome driving
the actual form): qualified → Telegram `message_id: 11`, not-qualified → no
send, unscored → manual-review `message_id: 12`, duplicate → 409, invalid →
client-side block plus an authoritative 422, FastAPI stopped → 502 with no
row and no notification.

## Deployment

Deployment configuration lives under `deploy/` and is documented in full in
[`docs/deployment.md`](docs/deployment.md) (architecture, provisioning
sequence, env files, health semantics, CORS model, failure drills, backup,
and an explicit verified/unverified ledger).

```bash
cp deploy/env/api.env.example  deploy/env/api.env    # DATABASE_URL, …
cp deploy/env/n8n.env.example  deploy/env/n8n.env    # public URLs, key, chat id
cp deploy/env/form.env.example deploy/env/form.env   # webhook URL for the form
deploy/bin/import-workflow.sh  # offline, before the first start
deploy/bin/stack.sh start      # or the systemd units in deploy/systemd/
deploy/bin/healthcheck.sh      # exit 0 = everything usable, not just running
deploy/bin/smoke.sh            # end-to-end through the real webhook (1 real Telegram)
```

Shape: **public HTTPS (Caddy) → same-origin form + `/webhook/*` → n8n
(loopback) → FastAPI (loopback) → PostgreSQL → Telegram**. Two supervised
processes, one proxy, one database — no containers, queue or microservices.
Secrets are env-file/credential-store only (`deploy/env/*.env` and `var/`
are gitignored; only `CHANGE_ME` templates are tracked).

**Status: deployment-ready — yes; public deployment personally verified —
no.** Everything was validated on a production-shaped local stack (fresh
database, fresh n8n folder, reverse proxy, failure drills, browser runs,
`pytest` 83 passed); no domain, TLS issuance or remote host was involved.

## Configuration

Read from the environment or `.env` (`.env` is gitignored; `.env.example` is
the committed template). Secrets are never printed, logged or returned.
Deployment splits the same variables into three gitignored env files with
committed `CHANGE_ME` templates — `deploy/env/api.env` (this table),
`deploy/env/n8n.env` and `deploy/env/form.env` (table below and
[`docs/deployment.md`](docs/deployment.md)).

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | yes | — | Application PostgreSQL DSN |
| `TEST_DATABASE_URL` | tests only | — | DSN the test suite runs against |
| `LLM_PROVIDER` | no | `openrouter` | `openrouter` or `openai-compatible` |
| `LLM_BASE_URL` | no | provider default | OpenAI-compatible endpoint |
| `LLM_MODEL` | no | `openrouter/free` | Configured model/router |
| `LLM_API_KEY` | no | *(unset → heuristic)* | Provider credential; never returned or logged |

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
demo/lead-form.html  single-file demo contact form → n8n webhook (config.js points it)
demo/config.js       THE single place the form's webhook URL lives (local default)
deploy/              env templates (.example), run/stack/import/render/
                     healthcheck/smoke/migrate scripts, systemd units, Caddyfiles
tests/               83 integration + unit tests
docs/requirements.md source of truth for project scope
docs/n8n-integration.md  the n8n contract and operating guide
docs/deployment.md   deployment guide, failure drills, verified/unverified ledger
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
the operational/audit layer (Milestone 9), Telegram notifications for the
two actionable outcomes (Milestone 10), a local demo lead form that feeds
the same webhook (Milestone 11) and a reproducible single-client deployment
(Milestone 12: systemd units, Caddy reverse proxy, externalized env files,
healthcheck/smoke commands and verified failure semantics — see
[`docs/deployment.md`](docs/deployment.md)).

**Deliberately out of scope:** authentication, rate limiting, Docker, CRM,
background workers, an independently designed production frontend (the
`demo/` form is the lead source and is rendered by the deployment),
email/Slack and any notification channel or
destination beyond the single configured Telegram chat, status
transition-graph enforcement, retries/queues, multi-workflow automation.
Deployment status is stated honestly in
[`docs/deployment.md`](docs/deployment.md): **deployment-ready, not publicly
deployed** — no domain, TLS issuance or remote host was involved.

**Next:** more n8n workflows on top of this boundary — additional
notification channels/rules, lead routing and CRM handoff.
