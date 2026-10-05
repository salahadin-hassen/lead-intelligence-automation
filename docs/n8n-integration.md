# n8n ↔ FastAPI integration (Milestone 7)

This document describes the first automation boundary between **n8n** and the
FastAPI lead API: one importable workflow, `n8n/lead-intake.json`, that accepts
an external lead over a webhook, forwards it to `POST /leads`, and returns the
*real* downstream result to the caller.

n8n orchestrates. FastAPI decides. Nothing about validation, persistence,
duplicate detection or scoring is duplicated in n8n.

## Flow

```
External lead source
        │  POST /webhook/lead-intake  (JSON body)
        ▼
  Lead Webhook            (n8n-nodes-base.webhook, responseMode: responseNode)
        ▼
  Prepare Lead Payload    (code node: resolve base URL + map contract fields)
        ▼
  Create Lead in FastAPI  (HTTP Request → {apiLeadsUrl})
        ▼
  Route on API Status     (switch on statusCode: 201 · 409 · 422 · fallback)
        ▼
  Respond Created | Respond Duplicate | Respond Validation Failed
              | Respond Upstream Error        (respondToWebhook)
        ▼
  caller receives an envelope that mirrors the API result
```

| Node | Responsibility |
|---|---|
| `Lead Webhook` | Receives the POST; waits for a Respond node (`responseMode: responseNode`) so the caller always gets an answer from the branch that actually ran. |
| `Prepare Lead Payload` | Resolves the API base URL (see Configuration) into `apiLeadsUrl`, then copies `name`, `email`, `company`, `message`, `source`, `external_id` out of the webhook body, drops unknown fields, defaults `source` to `n8n-webhook` when the caller omits it. **No validation rules are re-implemented here.** |
| `Create Lead in FastAPI` | `POST {apiLeadsUrl}` with a JSON body. `fullResponse` so the status code survives, `neverError` so a 409/422 continues down the graph instead of failing the execution, `onError: continueRegularOutput` so an unreachable API still reaches the fallback branch, `timeout: 10000`, **no retries**. |
| `Route on API Status` | Branches on the HTTP status FastAPI actually returned; anything else (500, 503, connection refused, timeout) goes to the fallback output. |
| `Respond *` | One Respond-to-Webhook node per outcome, each with its own response code and envelope. |

## Configuration

| Variable (n8n process env) | Required | Default | Purpose |
|---|---|---|---|
| `LEAD_API_BASE_URL` | production | `http://localhost:8000` (constant at the top of the `Prepare Lead Payload` code node) | Base URL of the FastAPI service, **without** a trailing `/leads` |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE` | no | *(unset = blocked on n8n 2.x)* | Set to `false` to let the workflow read `LEAD_API_BASE_URL` |

How the workflow reads it (inside `Prepare Lead Payload`, not in an
expression):

```js
const DEFAULT_API_BASE_URL = 'http://localhost:8000';
let apiBaseUrl = DEFAULT_API_BASE_URL;
let apiBaseUrlSource = 'workflow-default';
try {
  const fromEnv = $env.LEAD_API_BASE_URL;      // n8n's env accessor
  if (fromEnv) { apiBaseUrl = String(fromEnv).replace(/\/+$/, ''); apiBaseUrlSource = 'environment'; }
} catch (err) {
  apiBaseUrlSource = 'workflow-default (n8n blocks $env access)';
}
```

This shape was chosen because of how n8n 2.x actually behaves — verified
against the installed n8n **2.41.7**, not assumed:

- **`$env` is blocked by default.** `createEnvProviderState()` sets
  `isEnvAccessBlocked = process.env.N8N_BLOCK_ENV_ACCESS_IN_NODE !== 'false'`,
  so *any* value other than the literal `false` (including leaving it unset)
  makes `$env.X` throw `access to env vars denied`. The `try/catch` keeps the
  workflow alive and falls back to the constant instead of failing the
  execution.
- **`apiBaseUrlSource` is written to every execution's output**, so you can
  see which source was used. If it reports `workflow-default (n8n blocks $env
  access)` while you expected your environment variable, set
  `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` and restart n8n.
- To use the environment variable: `export LEAD_API_BASE_URL=http://fastapi:8000`
  in the n8n process environment (Docker Compose, systemd, shell) and set the
  blocking flag to `false`.
- To change it without touching the environment: edit the
  `DEFAULT_API_BASE_URL` constant at the top of the `Prepare Lead Payload` node.

No credentials are embedded in the JSON. The lead API is unauthenticated
today (same as every other endpoint in this service), so the workflow needs no
n8n credential either. When authentication is added later, attach it to the
HTTP Request node through n8n credentials — not by editing this file.

## Integration contract (as implemented)

Taken from `app/models.py`, `app/routes/leads.py` and `app/scoring.py` — not
from memory.

**Request** — `POST /leads`, `Content-Type: application/json`

| Field | Type | Rules |
|---|---|---|
| `name` | string | required, non-empty after trimming |
| `email` | string | required, valid email (`pydantic.EmailStr`) |
| `company` | string | required, non-empty |
| `message` | string | required, non-empty |
| `source` | string | required, non-empty |
| `external_id` | string \| null | optional; uniqueness is on `(source, external_id)` |

| API result | Code | Body |
|---|---|---|
| created | `201` | `LeadResponse` (`lead_id`, `name`, `email`, `company`, `source`, `status`, `created_at`, `score`, `score_reason`, `scored_at`) |
| duplicate `(source, external_id)` | `409` | `{"detail": "A lead with this source and external_id already exists"}` |
| validation failure | `422` | `{"detail": [{"loc": [...], "msg": "...", "type": "..."}, ...]}` (FastAPI default) |
| scoring failure | `201` | lead still created, `score` fields `null` — intake never fails because of scoring |

**Webhook response envelope** — one shape for every outcome:

```json
{"outcome": "...", "apiStatus": <FastAPI status>, ...}
```

| `outcome` | Webhook code | Extra fields | Meaning |
|---|---|---|---|
| `created` | `201` | `lead` — the full `LeadResponse` | FastAPI accepted and stored the lead |
| `duplicate` | `409` | `detail` | same `(source, external_id)` already exists; no second row |
| `validation_failed` | `422` | `detail` — FastAPI's field errors, passed through | payload rejected before persistence |
| `upstream_error` | `502` | `detail` — generic; `apiStatus` is `null` when no response arrived | API unreachable/timeout, or an unexpected status; the raw response stays in the n8n execution |

`apiStatus` always reports what FastAPI returned (`null` if the request never
completed), so an execution is inspectable even when the webhook answer is
generic. Success is never fabricated: the caller's `201` exists only when
PostgreSQL stored the row.

### Examples

```bash
# success
curl -s -X POST localhost:5678/webhook/lead-intake \
  -H 'Content-Type: application/json' \
  -d '{"name":"Ada Lovelace","email":"ada@example.com",
       "company":"Analytical Engines Ltd",
       "message":"Please send pricing for the enterprise plan.",
       "source":"partner-referral","external_id":"ext-9001"}'
# {"outcome":"created","apiStatus":201,"lead":{"lead_id":42,"status":"new",...}}

# duplicate  → 409 {"outcome":"duplicate","apiStatus":409,"detail":"..."}
# missing email → 422 {"outcome":"validation_failed","apiStatus":422,"detail":[...]}
# FastAPI stopped → 502 {"outcome":"upstream_error","apiStatus":null,
#                        "detail":"The lead API did not accept the request"}
```

Note that `external_id` is what makes a retry idempotent: re-sending the same
`(source, external_id)` yields `409`/`duplicate`, never a second row.

## Importing and activating

```bash
# 1. import (n8n on PATH, or npx n8n …)
n8n import:workflow --input=n8n/lead-intake.json

# 2. activate — on n8n 2.x a workflow must be *published* to serve
#    production webhook URLs ("active" comes from the published version)
n8n publish:workflow --id=lead-intake

# restart n8n if it is already running: n8n only registers the webhooks
# of published workflows at startup

# alternative: UI → Workflows → ⋯ → Import from File → n8n/lead-intake.json,
# then open it and toggle it Active.
```

The file ships a stable `"id": "lead-intake"`, which makes the CLI import an
**upsert**: re-running it updates the same workflow instead of creating a
second one. (`n8n import:workflow` on a single file requires an `id`; without
it the CLI fails with `NOT NULL constraint failed: workflow_entity.id`.)

| URL | When it works |
|---|---|
| `POST {n8n}/webhook/lead-intake` | workflow **published/Active** (production executions) |
| `POST {n8n}/webhook-test/lead-intake` | workflow open in the editor and "Listen for test event" pressed (one-shot) |

Local development assumptions: FastAPI on `localhost:8000`
(`.venv/bin/uvicorn app.main:app`), n8n on `localhost:5678`, both on the same
machine, PostgreSQL reachable by FastAPI. Nothing else is required — no queue,
no Redis, no credentials.

Two n8n 2.x port facts worth knowing (both hit while testing this workflow):

- n8n's task-runner **broker listens on 5679 by default**, independent of
  `N8N_PORT`. If you move n8n to another main port, 5679 must still be free or
  you must set `N8N_RUNNERS_BROKER_PORT`.
- `n8n import:workflow` / `publish:workflow` operate on n8n's own database
  (SQLite by default, in `N8N_USER_FOLDER`).

## Scope

In scope: this single intake workflow and the boundary it defines.

Out of scope for this milestone (later work): email/Telegram/Slack
notifications, CRM handoff, enrichment, scheduled follow-ups, retries and
queues, authentication, rate limiting, Docker, additional workflows.

## Verification

**This workflow was executed against a live n8n instance**, not only parsed.
Environment: n8n `2.41.7` (nodes-base `2.41.5`) installed under `/tmp`,
imported with `n8n import:workflow` + `n8n publish:workflow`, FastAPI served by
uvicorn against PostgreSQL 16.

| # | Scenario | Expected | Observed |
|---|---|---|---|
| 1 | valid lead, `LEAD_API_BASE_URL` set (env allowed) | `201 created`, row persisted | `201`, `lead_id` present, score 85 — row landed in the DB the env var pointed at |
| 2 | same `(source, external_id)` again | `409 duplicate` + FastAPI detail | `409`, detail passed through verbatim |
| 3 | payload with `email: null` | `422 validation_failed` + field errors | `422`, FastAPI `detail[0]` passed through |
| 4 | FastAPI process stopped | `502 upstream_error`, `apiStatus: null` | exactly that — no fabricated success |
| 5 | FastAPI restarted, same payload | `201` | `201`, single row (no double submit) |
| 6 | upstream returns HTTP `500` | `502 upstream_error`, `apiStatus: 500` | exactly that (Switch fallback branch) |
| 7 | n8n default config (`$env` blocked) while `LEAD_API_BASE_URL` is set | workflow falls back to `DEFAULT_API_BASE_URL` | row landed in the *other* database — proving the constant was used, not the env var |

Also verified during the work: n8n's own `validateWorkflowStructure` accepted
the JSON at import time; node parameter names/`typeVersion`s were checked
against the installed `n8n-nodes-base` sources (webhook `2.1`, code `2`,
httpRequest `4.5`, switch `3.4`, respondToWebhook `1.5`).

**Not tested:** running n8n in queue/multi-main mode, Dockerised n8n,
`webhook-test` (editor) URLs, and any credential/auth flow (none is used).
The backend contract itself is covered by the 83 pytest tests.
