# n8n ↔ FastAPI integration (Milestones 7 + 8)

This document describes the automation boundary between **n8n** and the
FastAPI lead API: one importable workflow, `n8n/lead-intake.json`, that accepts
an external lead over a webhook, forwards it to `POST /leads`, returns the
*real* downstream result to the caller (Milestone 7), and — only for leads
FastAPI actually created — turns FastAPI's score into a deterministic
qualification decision with visibly divergent routing actions (Milestone 8).

n8n orchestrates. FastAPI decides. Nothing about validation, persistence,
duplicate detection or scoring is duplicated in n8n: the workflow *reads* the
score FastAPI produced and labels it; it never computes, corrects or invents
one.

## Flow

```
External lead source
        │  POST /webhook/lead-intake  (JSON body)
        ▼
  Lead Webhook            (n8n-nodes-base.webhook, responseMode: responseNode)
        ▼
  Prepare Lead Payload    (code: config block — base URL + threshold —
                           then map contract fields)
        ▼
  Create Lead in FastAPI  (HTTP Request → {apiLeadsUrl})
        ▼
  Route on API Status     (switch on statusCode: 201 · 409 · 422 · fallback)
        │
        ├─ 201 ──┬─► Respond Created ─────────────────► caller gets the envelope
        │        └─► Extract Lead + Score   normalize LeadResponse (leadId,
        │                  │                 company, source, score …)
        │                  ▼
        │           Qualification          score vs threshold → label
        │                  ▼
        │           Route on Qualification (switch on `qualification`)
        │             ├─ qualified     ─► Log Priority Route
        │             ├─ not_qualified ─► Log Normal Route
        │             ├─ manual_review ─► Log Manual Review
        │             └─ (fallback)    ─► Log Manual Review
        ├─ 409 ───► Respond Duplicate
        ├─ 422 ───► Respond Validation Failed
        └─ other ──► Respond Upstream Error
        ▼
  caller receives an envelope that mirrors the API result
```

The 201 branch **fans out**: `Respond Created` answers the caller exactly as
in Milestone 7 (the webhook envelope is byte-identical), while
`Extract Lead + Score → Qualification → …` runs independently. A defect in
the qualification chain can therefore never change or delay the caller's
answer, and the qualification chain does not depend on execution continuing
past a `respondToWebhook` node.

| Node | Responsibility |
|---|---|
| `Lead Webhook` | Receives the POST; waits for a Respond node (`responseMode: responseNode`) so the caller always gets an answer from the branch that actually ran. |
| `Prepare Lead Payload` | **The workflow's single configuration source**: resolves the API base URL *and* the qualification threshold (see Configuration) into its output, then copies `name`, `email`, `company`, `message`, `source`, `external_id` out of the webhook body, drops unknown fields, defaults `source` to `n8n-webhook` when the caller omits it. **No validation rules are re-implemented here.** |
| `Create Lead in FastAPI` | `POST {apiLeadsUrl}` with a JSON body. `fullResponse` so the status code survives, `neverError` so a 409/422 continues down the graph instead of failing the execution, `onError: continueRegularOutput` so an unreachable API still reaches the fallback branch, `timeout: 10000`, **no retries**. |
| `Route on API Status` | Branches on the HTTP status FastAPI actually returned; anything else (500, 503, connection refused, timeout) goes to the fallback output. Only the 201 branch continues into qualification. |
| `Respond *` | One Respond-to-Webhook node per outcome, each with its own response code and envelope. |
| `Extract Lead + Score` | Reads the 201 body of `Create Lead in FastAPI` and normalizes it into the routing base result (`leadId`, `company`, `source`, `score`, `scoreReason`). The score is copied, not interpreted: anything FastAPI did not return as a finite number stays `null` (never `0`, never a guess). |
| `Qualification` | **The one place the threshold rule is applied** (see Qualification rule). Labels the lead `qualified` / `not_qualified` / `manual_review` and attaches `priority` (`high` / `normal` / `null`). Reads the threshold from `Prepare Lead Payload` — it does not define or repeat it. |
| `Route on Qualification` | Switch (string equality on `qualification`) with a defensive fallback output; the fallback — a state the decision node should never emit — is wired to `Log Manual Review`, so an unknown state goes to a human instead of being dropped. |
| `Log Priority Route` / `Log Normal Route` / `Log Manual Review` | Terminal notification nodes; each emits one structured record as its node output, which n8n persists in the execution data (see Notification). |

## Configuration

| Variable (n8n process env) | Required | Default | Purpose |
|---|---|---|---|
| `LEAD_API_BASE_URL` | production | `http://localhost:8000` (constant at the top of the `Prepare Lead Payload` code node) | Base URL of the FastAPI service, **without** a trailing `/leads` |
| `LEAD_QUALIFIED_THRESHOLD` | no | `70` (`DEFAULT_QUALIFIED_THRESHOLD`, same code node) | Score at which a lead is `qualified` (integer 0–100; out-of-range/invalid values are ignored and the default is used) |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE` | no | *(unset = blocked on n8n 2.x)* | Set to `false` to let the workflow read `LEAD_API_BASE_URL` / `LEAD_QUALIFIED_THRESHOLD` |

**One configuration source.** Both settings live in the configuration block at
the top of the `Prepare Lead Payload` node, and both are resolved the same way
(the `try/catch` `$env` probe with a documented constant fallback). No other
node reads `$env` or re-declares either default. Downstream, `Qualification`
takes the resolved value from that node's output:

```js
const config = $('Prepare Lead Payload').first().json;
const threshold = config.qualifiedThreshold;   // never redefined here
```

Every execution records which source was actually used:
`apiBaseUrlSource` and `thresholdSource` both appear in the
`Prepare Lead Payload` output (`workflow-default (n8n blocks $env access)` is
the value you get on a stock n8n 2.x).

How the base URL lookup looks (inside `Prepare Lead Payload`, not in an
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

The threshold probe is the same shape, plus a range/`Number.isFinite` check
(0–100); an invalid `LEAD_QUALIFIED_THRESHOLD` falls back to `70` and reports
`workflow-default (invalid LEAD_QUALIFIED_THRESHOLD)`.

This shape was chosen because of how n8n 2.x actually behaves — verified
against the installed n8n **2.41.7**, not assumed:

- **`$env` is blocked by default.** `createEnvProviderState()` sets
  `isEnvAccessBlocked = process.env.N8N_BLOCK_ENV_ACCESS_IN_NODE !== 'false'`,
  so *any* value other than the literal `false` (including leaving it unset)
  makes `$env.X` throw `access to env vars denied`. The `try/catch` keeps the
  workflow alive and falls back to the constant instead of failing the
  execution.
- **The `*Source` fields are written to every execution's output**, so you can
  see which source was used. If they report `workflow-default (n8n blocks $env
  access)` while you expected your environment variable, set
  `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` and restart n8n.
- To use the environment variables: export them in the n8n process environment
  (Docker Compose, systemd, shell) and set the blocking flag to `false`.
- To change a setting without touching the environment: edit the corresponding
  `DEFAULT_*` constant at the top of the `Prepare Lead Payload` node.

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

## Qualification rule (Milestone 8)

The decision is computed **once**, in the `Qualification` code node, and then
switched on as a plain string — so there is exactly one implementation of the
rule in the workflow:

```js
// score is not a finite number (null / missing)  → manual_review, priority null
// score >= threshold                             → qualified,     priority "high"
// score <  threshold                             → not_qualified, priority "normal"
```

| Input | `qualification` | `priority` | Terminal node |
|---|---|---|---|
| `score >= threshold` | `qualified` | `high` | `Log Priority Route` |
| `score < threshold` | `not_qualified` | `normal` | `Log Normal Route` |
| `score` not a finite number (`null` from FastAPI) | `manual_review` | `null` | `Log Manual Review` |
| anything else (defensive fallback output) | — | — | `Log Manual Review` |

Properties worth stating explicitly:

- **FastAPI owns the score.** The workflow compares; it never recomputes,
  never re-derives a score from the payload, and never writes to the backend's
  `status` field (that vocabulary — `new/contacted/qualified/closed` — is the
  manual PATCH workflow of Milestone 5; `qualification` is a separate,
  internal routing label).
- **`null` stays `null`.** A lead FastAPI could not score (LLM failure with a
  key configured, per Milestone 4's no-fallback rule) reaches
  `Log Manual Review` with `score: null` in the record — it is never treated
  as `0` and never silently classified as not-qualified.
- **A missing/misconfigured threshold also goes to manual review** rather than
  being guessed: if `qualifiedThreshold` from the config node is not a finite
  number, the decision node labels the lead `manual_review` and reports
  `qualifiedThreshold: null`.
- **`score == threshold` qualifies** (`>=`); verified live with score 95 at
  threshold 95.

### Threshold: 70 (initial business rule, not validated)

`70` is a **configurable starting point**, not an empirically validated sales
threshold. It was chosen from the observed score distribution of the offline
heuristic scorer (baseline 45, see `app/scoring.py`) over representative
leads:

```
junk / free-mail / low-intent   12  15  35
corporate, no intent keyword    63  63
explicit intent keyword(s)      77  91  93 100
                                └─ natural gap 63 → 77 ─┘
```

70 sits inside that gap: junk and "corporate but no buying signal" fall below
it, any explicit intent keyword (which the heuristic rewards with +5 each, up
to +30 on top of the 45 baseline) pushes a lead above it. **No conversion or
sales-qualified-lead data exists in this project**, so the rule has not been
validated against outcomes — treat it as a placeholder to revisit once real
pipeline data is available. It is tuned by editing `DEFAULT_QUALIFIED_THRESHOLD`
in the config block or setting `LEAD_QUALIFIED_THRESHOLD` (single source,
see Configuration).

## Notification (routing actions)

There is no email/Slack/Telegram/CRM integration in this environment and none
was invented for this milestone. Each terminal node therefore performs its
routing action by **emitting one structured record as its node output**, which
n8n persists in the execution data (and displays in the execution view) — a
workflow-native, inspectable delivery mechanism requiring no credentials or
third-party accounts:

| Terminal node | Record |
|---|---|
| `Log Priority Route` | `{"action": "priority_route", "leadId", "company", "source", "score", "qualification": "qualified", "priority": "high", "qualifiedThreshold"}` |
| `Log Normal Route` | `{"action": "normal_route", "leadId", "company", "source", "score", "qualification": "not_qualified", "priority": "normal", "qualifiedThreshold"}` |
| `Log Manual Review` | `{"action": "manual_review", "leadId", "company", "source", "score": null, "qualification": "manual_review", "priority": null, "note": "scoring unavailable - manual review required", "qualifiedThreshold"}` |

When a real notification channel (email, chat, CRM webhook) is added later,
it attaches to these three nodes — the record shape above is the payload.

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

In scope: this single intake workflow (intake + qualification routing) and the
boundary it defines.

Out of scope for these milestones (later work): delivering notifications
anywhere outside n8n's own execution data (email/Telegram/Slack/CRM — the
routing records are ready to be consumed by such a channel later), enrichment,
scheduled follow-ups, retries and queues, authentication, rate limiting,
Docker, additional workflows.

## Verification

### Milestone 8 — run personally against a live n8n instance

Environment: n8n `2.41.7` (nodes-base `2.41.5`) under `/tmp`, workflow
imported with `n8n import:workflow` + activated with `n8n publish:workflow
--id=lead-intake`, FastAPI served by uvicorn against PostgreSQL 16 (test
database, heuristic scoring — no `OPENAI_API_KEY` set). Branch outcomes were
read from n8n's own persisted execution data (SQLite `execution_data` table),
i.e. the same artifact an operator would inspect — not from the webhook
response, which never carries the routing result.

| # | Scenario | Expected | Observed |
|---|---|---|---|
| 1 | qualified lead (intent keywords + corporate domain + referral source → score 95), default threshold 70 | `201` + `qualified` → `Log Priority Route` | exec 14: `201`, `qualification: "qualified"`, `priority: "high"`, record `{"action":"priority_route","leadId":1,"score":95,…}` |
| 2 | not-qualified lead (free-mail, newsletter source, one-word message → score 12), default threshold 70 | `201` + `not_qualified` → `Log Normal Route` | exec 15: `201`, `qualification: "not_qualified"`, `priority: "normal"`, record `{"action":"normal_route","leadId":2,"score":12,…}` |
| 3 | unscored lead: second FastAPI run with `OPENAI_API_KEY` set (bogus) and `OPENAI_BASE_URL` on a dead port → M4 rule returns `score: null` | `201` with `score: null` + `manual_review` → `Log Manual Review`, score stays `null` | exec 20: `201`, `"score":null`, record `{"action":"manual_review","leadId":5,"score":null,"note":"scoring unavailable - manual review required"}` |
| 4 | duplicate `(source, external_id)` | `409` and **no** qualification nodes run | exec 16: `409`, nodes = …→ `Respond Duplicate` only (Extract/Qualification never executed) |
| 5 | payload missing `email` | `422` and **no** qualification nodes run | exec 17: `422`, `Respond Validation Failed` only |
| 6 | FastAPI process stopped | `502` (`apiStatus: null`) and **no** qualification nodes run | exec 18: `502`, `Respond Upstream Error` only |
| 7 | threshold flip: restart n8n with `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` + `LEAD_QUALIFIED_THRESHOLD=95`, re-send the **same score-75 payload** that qualified at threshold 70 (scenario 9, exec 19) | `not_qualified` (75 < 95), `thresholdSource: "environment"` | exec 22: `qualification: "not_qualified"`, `qualifiedThreshold: 95`, `thresholdSource: "environment"` |
| 8 | score exactly equal to threshold (95 at threshold 95) | `qualified` (`>=` semantics) | exec 23: `qualification: "qualified"`, `priority: "high"` |
| 9 | boundary lead at default threshold (score 75 ≥ 70) | `qualified` | exec 19: `Log Priority Route`, `qualifiedThreshold: 70`, `thresholdSource: "workflow-default (n8n blocks $env access)"` |

The webhook envelopes for scenarios 1–6 are byte-identical to Milestone 7's
(the 201 branch still runs `Respond Created` first; qualification runs on a
parallel fan-out).

Also verified for this milestone: a structural validator over the JSON
(unique ids/names, all connection targets resolve, single webhook trigger,
reachability from the trigger, only intended terminal nodes, M7 nodes and
non-201 connections byte-identical to `HEAD`, config/threshold defined exactly
once, no secrets or absolute paths, node `typeVersion`s present in the
installed `n8n-nodes-base` sources: webhook `2.1`, code `2`, httpRequest `4.5`,
switch `3.4`, respondToWebhook `1.5`); n8n's `validateWorkflowStructure`
accepted the JSON at import time; `pytest` → 83 passed (backend untouched).

### Milestone 7 — inherited (unchanged by this milestone)

The intake boundary itself was verified live in Milestone 7 and re-confirmed
as a regression check here (scenarios 1, 4, 5, 6 above reproduce its codes):

| # | Scenario | Expected | Observed (M7) |
|---|---|---|---|
| 1 | valid lead, `LEAD_API_BASE_URL` set (env allowed) | `201 created`, row persisted | `201`, `lead_id` present, score 85 — row landed in the DB the env var pointed at |
| 2 | same `(source, external_id)` again | `409 duplicate` + FastAPI detail | `409`, detail passed through verbatim |
| 3 | payload with `email: null` | `422 validation_failed` + field errors | `422`, FastAPI `detail[0]` passed through |
| 4 | FastAPI process stopped | `502 upstream_error`, `apiStatus: null` | exactly that — no fabricated success |
| 5 | FastAPI restarted, same payload | `201` | `201`, single row (no double submit) |
| 6 | upstream returns HTTP `500` | `502 upstream_error`, `apiStatus: 500` | exactly that (Switch fallback branch) |
| 7 | n8n default config (`$env` blocked) while `LEAD_API_BASE_URL` is set | workflow falls back to `DEFAULT_API_BASE_URL` | row landed in the *other* database — proving the constant was used, not the env var |

**Not tested (either milestone):** running n8n in queue/multi-main mode,
Dockerised n8n, `webhook-test` (editor) URLs, credential/auth flows (none is
used), concurrent submissions to the same `(source, external_id)`, and any
real notification channel (none exists here — records stay in execution data).
The backend contract itself is covered by the 83 pytest tests.

**Known limitations:** the threshold is an unvalidated business default
(above); execution-data records are only visible to someone with access to the
n8n instance (UI or database) — nothing pushes them to a human; executions are
saved for success runs (`saveDataOnSuccess: all` default), and a *failed*
qualification node would surface as a failed execution status while the caller
still got its `201` (by design of the fan-out, but it means routing failures
are noticed only by monitoring, not by the caller).
