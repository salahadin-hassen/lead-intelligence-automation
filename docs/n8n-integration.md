# n8n ↔ FastAPI integration (Milestones 7–12)

This document describes the automation boundary between **n8n** and the
FastAPI lead API: one importable workflow, `n8n/lead-intake.json`, that accepts
an external lead over a webhook, forwards it to `POST /leads`, returns the
*real* downstream result to the caller (Milestone 7), turns FastAPI's score
into a deterministic qualification decision with visibly divergent routing
actions (Milestone 8), and — only for leads FastAPI actually created —
records each decision as a structured operational event an operator can
inspect in the execution data (Milestone 9). Milestone 10 then delivers the
two records a human must act on — `qualified` and `manual_review` — as real
Telegram messages to one configured chat, without touching the
caller-response isolation Milestone 9 established. Milestone 11 adds a local
single-file contact form (`demo/lead-form.html`) that feeds this exact
webhook as a real lead source — browser → n8n → FastAPI — changing no
backend contract. Milestone 12 wraps the same boundary in a reproducible
deployment (systemd + Caddy + env files) without changing the contract;
the deployment guide is [`docs/deployment.md`](deployment.md).

n8n orchestrates. FastAPI decides. Nothing about validation, persistence,
duplicate detection or scoring is duplicated in n8n: the workflow *reads* the
score FastAPI produced and labels it; it never computes, corrects or invents
one. The Telegram message is a human-readable **rendering** of the operational
record, never a substitute for it: the structured record stays authoritative
in the execution data alongside the message.

## Flow

```
Lead form (demo/lead-form.html) · curl · any other client
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
        │        │     (executes FIRST — see Caller-response isolation)
        │        └─► Extract Lead + Score   normalize LeadResponse (leadId,
        │                  │                 company, source, score …)
        │                  ▼
        │           Qualification          score vs threshold → label
        │                  ▼
        │           Route on Qualification (switch on `qualification`)
        │             ├─ qualified     ─► Log Priority Route   ─┐
        │             ├─ not_qualified ─► Log Normal Route      ─┤ operational
        │             ├─ manual_review ─► Log Manual Review     ─┤ records
        │             └─ (fallback)    ─► Log Manual Review     ─┘
        │                                        ▼
        │                              Build Audit Event    unified event
        │                                        ▼
        │                              (terminal, in execution data)
        ├─ 409 ───► Respond Duplicate
        ├─ 422 ───► Respond Validation Failed
        └─ other ──► Respond Upstream Error
        ▼
  caller receives an envelope that mirrors the API result

  Telegram notification chains (Milestone 10) — appended to two branch
  outputs only, positioned BELOW Build Audit Event on the canvas so the
  audit event always executes first (top-most sibling runs first):

    Log Priority Route ─► Build Qualified Notification ─► Send Qualified to Telegram
    Log Manual Review  ─► Build Manual Review Notification ─► Send Manual Review to Telegram

    not_qualified · 409 · 422 · 502  →  no Telegram node is reachable
```

The 201 branch **fans out**: `Respond Created` answers the caller exactly as
in Milestone 7 (the webhook envelope is byte-identical), while
`Extract Lead + Score → Qualification → … → Build Audit Event` runs
independently. A defect anywhere in the operational chain can therefore
neither change nor delay the caller's answer — but only because
`Respond Created` *executes first*, which is arranged by its canvas position
(see Caller-response isolation); the branch does not depend on execution
continuing past a `respondToWebhook` node.

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
| `Log Priority Route` / `Log Normal Route` / `Log Manual Review` | The three operational branches. Each emits its **operational record** (`event`, lead data, `reason`, threshold provenance) *plus a human-readable `summary`* as its node output, which n8n persists in the execution data (see Operational actions & audit event). |
| `Build Audit Event` | Terminal merge of whichever branch ran: adds `timestamp` (n8n's `$now`), `executionId` and `executionMode` to the record, producing the **unified audit event** — the structured hand-off point a consumer (email, Slack, CRM, analytics) would attach to. |
| `Build Qualified Notification` / `Build Manual Review Notification` | Milestone 10. Code nodes appended to `Log Priority Route` / `Log Manual Review`: render the **existing branch record** into a plain-text Telegram message (real fields only — company, lead ID, score, qualification, source, plus `reason` for manual review) and resolve the destination chat from `$env.TELEGRAM_CHAT_ID` (explicit throw if it is missing or `$env` is blocked). Output = the record **unchanged** plus exactly two added keys: `chatId`, `telegramMessage`. |
| `Send Qualified to Telegram` / `Send Manual Review to Telegram` | Milestone 10. `n8n-nodes-base.telegram` (`sendMessage`, typeVersion `1.2`), `chatId`/`text` as expressions off the build node's output, one `telegramApi` credential reference (id + name only — the token lives in n8n's credential store), **no `onError` override** so a delivery failure surfaces as a failed execution while the caller keeps its `201`. |

## Configuration

| Variable (n8n process env) | Required | Default | Purpose |
|---|---|---|---|
| `LEAD_API_BASE_URL` | production | `http://localhost:8000` (constant at the top of the `Prepare Lead Payload` code node) | Base URL of the FastAPI service, **without** a trailing `/leads` |
| `LEAD_QUALIFIED_THRESHOLD` | no | `70` (`DEFAULT_QUALIFIED_THRESHOLD`, same code node) | Score at which a lead is `qualified` (integer 0–100; out-of-range/invalid values are ignored and the default is used) |
| `TELEGRAM_CHAT_ID` | for notifications | — (no default: a destination must be configured) | Single destination chat for both Telegram sends; read via `$env` **inside the two build nodes**, which throw explicitly when it is unset or `$env` is blocked — a configuration error then shows up as a failed execution, never as a silent no-send and never as a caller error |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE` | no | *(unset = blocked on n8n 2.x)* | Set to `false` to let the workflow read `LEAD_API_BASE_URL` / `LEAD_QUALIFIED_THRESHOLD` **and** `TELEGRAM_CHAT_ID` |

**Deployment:** none of this is exported by hand — a deployment keeps these
values in the gitignored `deploy/env/n8n.env` (template:
`deploy/env/n8n.env.example`), loaded by the systemd unit or
`deploy/bin/stack.sh`; the form's webhook URL is configured once in
`deploy/env/form.env`. See [`docs/deployment.md`](deployment.md).

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

No secrets are embedded in the JSON. The lead API is unauthenticated today
(same as every other endpoint in this service), so the HTTP Request node needs
no n8n credential. The two Telegram nodes reference exactly one n8n
credential — type `telegramApi`, `{"id": "telegram-lead-alerts", "name":
"Telegram Lead Alerts"}` — which is the **non-secret reference n8n requires**;
the bot token itself lives only in n8n's credential store (imported via
`n8n import:credentials`, stored encrypted) and in a chmod-600 file outside
this repository, never in a committed file. When API authentication is added
later, attach it to the HTTP Request node through n8n credentials — not by
editing this file.

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

## Operational actions & audit event (Milestone 9)

Each qualification branch performs its operational action by **emitting a
structured record as its node output**; the three records then merge into one
unified audit event. Everything stays inside n8n's execution data — no
database table, no FastAPI endpoint, no queue.

> **Milestone 9 itself delivered nothing external — these were workflow-native
> operational records, not notifications.** Milestone 10 (see *Telegram
> notifications* below) adds the first real delivery: a Telegram message for
> the `qualified` and `manual_review` branches only, rendered from the very
> records described here. Email/Slack/CRM integrations still do not exist in
> this environment and none is faked. The structured records remain the
> authoritative artifact — a Telegram message is a human-readable rendering of
> the same values, never a replacement for them.

### Record shape

Every branch emits the same keys, so a consumer never has to branch on shape:

| Key | Meaning |
|---|---|
| `event` | `lead_qualified` · `lead_not_qualified` · `lead_manual_review` — the operational action type |
| `leadId`, `company`, `source` | copied from FastAPI's 201 body, never re-derived |
| `score` | FastAPI's score; `null` when it could not score — never fabricated |
| `scoreReason` | FastAPI's `score_reason` sentence (the audit "why") |
| `qualification`, `priority` | the decision computed by `Qualification` (Milestone 8) |
| `reason` | `null` for qualified/not-qualified; `scoring unavailable` or `qualification could not be determined` for manual review |
| `qualifiedThreshold`, `thresholdSource` | which threshold was applied and where it came from |
| `summary` | human-readable rendering of the same values |

Example — the `Log Priority Route` output of a qualified lead:

```json
{
  "event": "lead_qualified",
  "leadId": 123,
  "company": "Example Corp",
  "source": "website",
  "score": 95,
  "scoreReason": "High-intent keywords (demo, pricing); corporate email domain",
  "qualification": "qualified",
  "priority": "high",
  "reason": null,
  "qualifiedThreshold": 70,
  "thresholdSource": "workflow-default (n8n blocks $env access)",
  "summary": "HIGH PRIORITY LEAD\nCompany: Example Corp\nLead ID: 123\nScore: 95\nSource: website\nQualification: qualified"
}
```

### The three actions

| Branch | Terminal node | `event` | `priority` | `summary` header |
|---|---|---|---|---|
| `score >= threshold` | `Log Priority Route` | `lead_qualified` | `high` | `HIGH PRIORITY LEAD` |
| `score < threshold` | `Log Normal Route` | `lead_not_qualified` | `normal` | `NOT QUALIFIED` |
| score `null`, unusable threshold, or the switch's defensive fallback | `Log Manual Review` | `lead_manual_review` | `null` | `MANUAL REVIEW REQUIRED`, plus a `Reason:` line |

The human-readable form is a `summary` **string field on the same output
JSON** — the n8n-native mechanism that survives into persisted execution data
and shows up in the execution view. Static node "notes" cannot carry
per-lead values, and `console.log` output is not persisted without
`CODE_ENABLE_STDOUT=true`, so neither is used:

```
MANUAL REVIEW REQUIRED
Company: Example Corp
Lead ID: 125
Score: null
Source: website
Qualification: manual_review
Reason: scoring unavailable
```

### The audit event

`Build Audit Event` runs after whichever branch fired and adds execution
context, producing **one unified downstream event per executed lead**:

| Key | Example / meaning |
|---|---|
| `timestamp` | `2026-10-05T10:54:07.425-04:00` — n8n's `$now.toISO()` |
| `executionId` | `"39"` — `$execution.id` |
| `executionMode` | `production` for webhook runs (`test` for manual editor runs) |
| … | the complete branch record above, unchanged |

Structured rather than flattened into one string, it is the hand-off point a
consumer (email, Slack, CRM, analytics) would attach to — after
`Build Audit Event` for a single stream, or after a specific branch when a
channel only wants one class of lead. Milestone 9 implemented none of those
channels and added no storage for the event; Milestone 10 attaches
Telegram — but to the **branch records** of the two actionable outcomes,
after the audit node has run, and the audit event itself stays storage-side
in the execution data.

### Caller-response isolation

The webhook answer must never depend on the operational chain. The mechanism
below was **measured, not assumed**:

- **n8n 2.x `executionOrder: v1` orders sibling nodes by canvas position.**
  After a node runs, its destination nodes are sorted by `position[1]`
  descending (bottom-most first) and each is `unshift`ed onto the execution
  stack — so the **top-most sibling executes first**, and the order of entries
  in the connection array is irrelevant.
- In Milestones 7/8 `Respond Created` sat *below* `Extract Lead + Score`
  (`[960,-240]` vs `[960,-480]`) and therefore ran **last** — the opposite of
  what this document previously claimed. Milestone 9 moves `Respond Created`
  to `[960,-720]` (above the first operational node) so the caller is
  answered **before** any qualification, branch or audit node runs. The
  connection list itself stays byte-identical to Milestone 7.
- Consequence: an operational failure surfaces as a **failed execution
  (`status: error`) with the error message persisted in the execution data** —
  visible, not swallowed — while the caller keeps the genuine `201`. No
  operational node sets `onError: continue*`, so failures are never muted.

Fault-injection evidence (Verification, cases 10–12): with the old position a
throwing `Extract Lead + Score` made the caller receive
`500 {"message":"Error in workflow"}` although FastAPI had already stored the
row; with the M9 position the same fault returns `201`, the execution is
`status=error` with the injected message recorded, and the lead row exists.

## Telegram notifications (Milestone 10)

The first real external business action of this workflow: the two outcomes a
human must act on — `qualified` and `manual_review` — render their existing
operational record as a plain-text Telegram message and deliver it through
n8n's Telegram node to one configured chat. Everything else stays silent.

### Notification policy

| Outcome | Telegram | Executed path |
|---|---|---|
| `qualified` | **send** | `Log Priority Route` → `Build Qualified Notification` → `Send Qualified to Telegram` |
| `manual_review` | **send** — a human must review a lead the system could not score or decide | `Log Manual Review` → `Build Manual Review Notification` → `Send Manual Review to Telegram` |
| `not_qualified` | **none** | `Log Normal Route` → `Build Audit Event` (terminal; no Telegram node reachable) |
| `409` duplicate / `422` validation / `502` upstream | **none** — qualification, audit and notification nodes never run | `Respond Duplicate` / `Respond Validation Failed` / `Respond Upstream Error` |

The two chains are **appended only** to `Log Priority Route` and
`Log Manual Review`; no existing node, parameter, position or connection was
modified (the artifact diff is purely additive, 0 deletions).

### Message content

Rendered **only** from the branch record's actual fields — `company`,
`leadId`, `score`, `qualification`, `source` (plus `reason` for manual
review). Nothing is fabricated or hardcoded. The build node's output is the
record **unchanged** plus exactly two keys (`chatId`, `telegramMessage`), so
the structured operational record stays authoritative and `score: null` stays
`null` — the string `unavailable` appears only inside the rendered message.

Qualified, exactly as delivered (Telegram API `ok: true`, `message_id: 9`,
execution 10):

```
🔥 HIGH-PRIORITY LEAD

Company: Harborline Systems
Lead ID: 8
Score: 95
Qualification: qualified
Source: website

Action: Review and contact this lead.
```

Manual review, exactly as delivered (`ok: true`, `message_id: 8`, execution 9;
the lead's structured `score` was `null`):

```
⚠️ LEAD NEEDS MANUAL REVIEW

Company: Meadowline
Lead ID: 7
Score: unavailable
Qualification: manual_review
Source: referral

Reason: scoring unavailable.
```

### Configuration & credentials

- **Destination:** `TELEGRAM_CHAT_ID` on the n8n process, read by the build
  nodes through `$env` (requires `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`).
  Missing or blocked → the build node **throws** → failed execution
  (visible), caller unaffected. Single destination by design: no recipient
  lookup, no user mapping, no multi-tenant or dynamic channels.
- **Bot token:** exists only in n8n's credential store — a `telegramApi`
  credential (id `telegram-lead-alerts`) imported with
  `n8n import:credentials` and stored **encrypted** (verified: the exported
  `data` field is ciphertext, not plaintext). The committed workflow carries
  only the non-secret `{id, name}` reference; no token, chat id or
  credential data appears in any committed file.
- **Telegram node settings** (source of truth: the `Telegram` node source in
  the installed `n8n-nodes-base` 2.41.5 that ships with n8n 2.41.7 —
  `version: [1, 1.1, 1.2]`): `typeVersion: 1.2`, `resource: message`,
  `operation: sendMessage`, `chatId: "={{ $json.chatId }}"` (string
  expression), `text: "={{ $json.telegramMessage }}"`,
  `additionalFields: {appendAttribution: false, parse_mode: "HTML"}`. No
  `onError` override: a failed send must surface as `status: error`.
- Two settings were **measured, not guessed**, and both encode real n8n 2.x
  behavior found by breaking the happy path first:
  - `parse_mode: "HTML"` — n8n *forces* Markdown when `parse_mode` is unset
    (`addAdditionalFields`), and Telegram then rejects the `_` in
    `manual_review` with `can't parse entities` (observed live, execution 8).
    HTML mode only treats `&<>` as special, so the build nodes HTML-escape
    interpolated values — a no-op for normal data (the delivered messages
    above are byte-identical to the policy examples).
  - `resource`/`operation`/`replyMarkup` are stored **explicitly** even
    though they have defaults: n8n's load-time parameter normalization drops
    collection keys whose `displayOptions` (`/operation: ['sendMessage']`)
    cannot resolve against a parameter set that has no `operation` — without
    them `appendAttribution: false` was silently reset to `{}` and n8n's
    "sent automatically with n8n" footer appeared (observed live,
    executions 1–2).

### Audit before notification

Both build nodes sit **below** `Build Audit Event` on the canvas
(`y = -400`/`-320` vs `-480`), and n8n 2.x `executionOrder: v1` runs the
top-most sibling first — so for either branch the order is
`Build Audit Event` → build notification → Telegram send. Verified from the
**persisted start timestamps**, not canvas position alone:

- execution 10 (qualified): audit `…168349 ms` → build `…168362` → send `…168374`
- execution 16 (fault run): audit `…369834` → build `…369846` → send `…369857` — the
  audit event is fully persisted **even though the send then failed**

### Caller isolation with Telegram in the chain

The M9 guarantee extends unchanged: `Respond Created` still executes before
qualification, audit and notification, and no notification node sets
`onError: continue*`. Deliberately verified by breaking Telegram (fault run,
execution 16): the `chatId` expression was replaced with an invalid literal
destination (no credential touched), then a qualified lead was sent —
the caller received a **genuine `201`** with the row stored, the execution
failed with `status: error` and `NodeApiError: Bad request … Bad Request:
chat not found` persisted, and the audit event ran before the failed send.
A Telegram failure can therefore never turn an accepted intake into a fake
`500`, and it is never swallowed.

Operational caveat found during that test: right after an import+publish+
restart, the webhook can briefly serve the **previously published** version
(execution 15 ran ~18 s after the reload on the old version and delivered a
real message); the faulted version was in effect 139 s later (execution 16).
Wait for the new publication to settle (or verify the active version) before
testing a freshly reloaded workflow.

## Demo lead form (Milestone 11)

```
Demo lead form          (demo/lead-form.html, plain HTML/CSS/JS)
      │  fetch() POST JSON  — direct, CORS preflight answered by n8n
      ▼
n8n webhook             (POST /webhook/lead-intake, unchanged workflow)
      ▼
FastAPI /leads          (unchanged contract)
      ▼
PostgreSQL + scoring → qualification → audit → Telegram
```

### Purpose & location

`demo/lead-form.html` — one self-contained file (no framework, no build step,
no dependencies) — turns the pipeline into a client-demoable story: a
prospective customer fills out a normal business contact form, and the
submission travels the *same* path every other lead source takes. It is a
**source** for the existing automation, not a second backend: no scoring,
qualification or validation rules exist in the frontend, the backend contract
did not change, and the workflow JSON was not touched by this milestone.

The form collects exactly the contract fields `name`, `email`, `company`,
`message` (plus the workflow-injected `source`); `external_id` is generated,
not asked for (see below).

### Running it

```bash
# prerequisites: PostgreSQL, FastAPI on :8000, n8n on :5679 with the
# workflow published (the same services this document describes)
python3 -m http.server 8001 --directory demo
# open http://localhost:8001/lead-form.html
```

Any static file server works; `python3 -m http.server` was chosen because it
is Python-standard-library and dependency-free. The page is **not deployed
anywhere** — it is a local demo artifact.

### Configuration

The webhook URL lives in exactly **one** place: the `CONFIG` object at the
top of the `<script>` block in `demo/lead-form.html`.

```js
const CONFIG = {
  webhookUrl: "http://localhost:5679/webhook/lead-intake", // this machine's n8n
  source: "website-contact-form",
};
```

- The URL appears nowhere else in the file; point it at a different n8n by
  editing that one line.
- `source` reuses the value the repository's API tests already send
  (`website-contact-form`) instead of inventing new vocabulary. It is not a
  user-editable field; the backend receives it as data on every submission.
- No credentials of any kind are embedded (the n8n webhook is unauthenticated
  exactly as before — M11 deliberately adds no webhook auth).

### CORS decision: direct browser → n8n (no proxy)

Tested against the running n8n **2.41.7** instance before any code was
written, not guessed:

| Probe | Result |
|---|---|
| `OPTIONS /webhook/lead-intake` with `Origin` + `Access-Control-Request-*` | `204`, `Access-Control-Allow-Methods: OPTIONS, POST`, `Access-Control-Allow-Origin: <origin>` (reflected), `Access-Control-Allow-Headers: content-type`, `Access-Control-Max-Age: 300` |
| `POST` with `Origin` | response carries `Access-Control-Allow-Origin: <origin>` |

The webhook answers cross-origin requests out of the box, so the form uses a
plain `fetch()` **directly against n8n**. In M11 no proxy existed anywhere:
no `demo/` server beyond a static file server, no second backend, no
FastAPI exposure, no n8n CORS settings changed.

**Milestone 12 added the deployment topology and made the CORS policy
explicit instead of implicit:**

- The workflow's Lead Webhook now carries
  `parameters.options.allowedOrigins = "http://localhost:8001"` — the local
  dev origin is **allowlisted** (this is the only change to
  `n8n/lead-intake.json` in M12). Probes against n8n 2.41.7: an allowed
  origin gets `204` + `Access-Control-Allow-Origin: http://localhost:8001`;
  a different origin (e.g. `http://evil.example`) still receives a
  *mismatched* ACAO (the option allowlists, it does not reflect), so the
  browser blocks the read; requests without an `Origin` header (curl,
  server-to-server) are unaffected — CORS is a browser mechanism, never
  authentication.
- In production the form is served from the **same origin** as
  `/webhook/*` (Caddy proxies `https://<domain>/webhook/*` to loopback n8n),
  so browsers never hit a cross-origin case at all: no preflight, no CORS
  headers relied upon. The browser drill asserted exactly that — the
  same-origin submission emitted **zero `OPTIONS` requests** and still got
  `201`.

### Response handling

The UI maps the workflow's real envelope — it never shows a blanket
"Success!" for any 2xx:

| Workflow response | Form state |
|---|---|
| `201 {"outcome":"created"}` | Success panel: *"Thanks. Your message has been received."* — no score/threshold/internal detail shown |
| `409 {"outcome":"duplicate"}` | Duplicate panel: *"We already have this message."* |
| `422 {"outcome":"validation_failed"}` | Friendly per-field errors derived from the response's field **names** only (`email` → *"Please enter a valid email address."*, other known fields → *"Please check this field."*, unknown → generic banner). Raw server text is never rendered |
| `502 {"outcome":"upstream_error"}`, network failure, or any unexpected response | Banner: *"We couldn't submit your request right now. Please try again."* — form values retained, no stack traces, no n8n/FastAPI internals (details may go to `console` in passing, never into the page) |

Client-side validation (required fields, email format) blocks obviously
invalid submissions before any request, with inline errors, `aria-invalid`,
focus moved to the first invalid field — while FastAPI's validation remains
the authority (verified: a browser-accepted but API-rejected email still got
the real `422`).

### `external_id` & duplicate demos

Normal submissions generate one `crypto.randomUUID()` per message, kept
across retries of that message and rotated after success. Purpose: an
accidental double-submit or retry cannot create a second row — the retry
re-uses the id and the server's `(source, external_id)` rule answers `409`
if the first attempt landed. This is browser-level convenience and is
**not** claimed as a globally unique identifier (collisions across sessions
are theoretically possible; irrelevant for a demo). The server's duplicate
semantics remain the only authority.

For deliberate duplicate demonstrations open
`lead-form.html?external_id=<fixed-id>` — the id is then pinned, so
submitting the same content twice renders the duplicate state. This is the
milestone's only "developer" affordance: it is hidden in the URL, harmless to
ordinary users, and exposes nothing internal.

### What the form must never reveal

No scoring formula, threshold, heuristic details, AI/OpenAI configuration,
database or n8n internals, execution IDs, Telegram details or stack traces
are rendered anywhere in the page; qualification happens entirely downstream.
Success means exactly *"Thanks. Your message has been received."*

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

**Use the shipped script for deployments:** `deploy/bin/import-workflow.sh`
runs the two steps offline (self-loads `deploy/env/n8n.env`, idempotent) and
tells you to restart afterwards.

**Publications are asynchronous — always import offline, then restart
(learned while validating M12 against n8n 2.41.7):** `publish:workflow`
enqueues a publication (`workflow_publication_outbox`) that the *running*
instance applies seconds later, and a freshly started instance first serves
its previously applied version before reconciling to the new
`activeVersionId` — executions in that window record the **old** content.
The CLI says as much (*"Changes will not take effect if n8n is running"*).
`deploy/bin/stack.sh start` therefore waits (up to 120 s) for the applied
pointer to equal the desired active version and the outbox to drain, and
fails loudly if it does not — "stack up" implies "the imported workflow is
what executes".

| URL | When it works |
|---|---|
| `POST {n8n}/webhook/lead-intake` | workflow **published/Active** (production executions) |
| `POST {n8n}/webhook-test/lead-intake` | workflow open in the editor and "Listen for test event" pressed (one-shot) |

Local development assumptions: FastAPI on `localhost:8000`
(`.venv/bin/uvicorn app.main:app`), n8n on `localhost:5678`, both on the same
machine, PostgreSQL reachable by FastAPI. Nothing else is required — no queue
and no Redis. The one credential the workflow uses (Telegram bot token) is
created once in n8n's credential store, not in this repository (see
Configuration).

Two n8n 2.x port facts worth knowing (both hit while testing this workflow):

- n8n's task-runner **broker listens on 5679 by default**, independent of
  `N8N_PORT`. If you move n8n to another main port, 5679 must still be free or
  you must set `N8N_RUNNERS_BROKER_PORT`.
- `n8n import:workflow` / `publish:workflow` operate on n8n's own database
  (SQLite by default, in `N8N_USER_FOLDER`).

## Scope

In scope: this single intake workflow (intake + qualification routing +
operational/audit records + Telegram delivery of the two actionable records),
the local demo lead form that feeds its webhook (Milestone 11), the
reproducible deployment of exactly this boundary (Milestone 12: env files,
systemd, Caddy same-origin fronting, healthcheck/smoke/failure drills — see
[`docs/deployment.md`](deployment.md)), and the boundary they define.

Out of scope for these milestones (later work): any delivery channel besides
the single-destination Telegram send of Milestone 10 (email/Slack/CRM/
webhook-out — Telegram was deliberately the first and only channel),
recipient lookup or multi-tenant/multi-channel notifications, persisting the
audit event (no table, no endpoint — that is deliberate), enrichment,
scheduled follow-ups, retries and queues, authentication, rate limiting,
Docker, additional workflows. A product frontend, accounts and any
multi-tenant hosting are out of scope as well: the form is this pipeline's
lead source — served locally by a throwaway static server in development
and by the deployment's rendered copy (`var/www` behind Caddy) when
deployed. No public hosting was performed; the verified/unverified ledger is
in [`docs/deployment.md`](deployment.md).

## Verification

### Milestone 12 — deployment rehearsal (local, production-shaped)

Environment: a fresh throwaway PostgreSQL 16 cluster (own data dir,
dedicated `lead_app` role — proves fresh-database startup migrates itself),
FastAPI on `:8010`, n8n 2.41.7 on `:5678` with a **pristine data folder**
(proves first-run provisioning: offline import → publish → start → webhook
registered), Caddy 2.11.7 on `:8080` via `deploy/caddy/Caddyfile.local`
(identical routing to production, plain HTTP), and the rendered form served
**same-origin** through the proxy. Two batteries, both green:

| Battery | Result |
|---|---|
| Non-destructive A: stack stop/start (registration-lag retry observed at attempt 1/6), Caddy form + `config.js` 200, `healthcheck.sh`, `migrate.sh` idempotent re-run, **smoke through the proxy 11/11** (not-qualified/qualified incl. real Telegram `ok:true`, duplicate `409`, invalid `422`, cleanup), CORS probes (allowed origin → `204`+matching ACAO; denied origin → mismatched ACAO; no-Origin POST processed), headless-Chrome same-origin submission → `201` with **zero `OPTIONS` preflights** + success panel + console `[lead-form] response: 201 created`, headless-Chrome missing-`config.js` → visible setup error + **0 POSTs**, restart persistence (`workflow: [('lead-intake',1)]`, `webhook: [('lead-intake','POST')]`) | all pass, exit 0 |
| Failure drills B: FastAPI down → `502 upstream_error` + no row + no Telegram + `Respond Upstream Error` execution; PostgreSQL down → `/health 503` + healthcheck fails naming `503` + full recovery after restart; Telegram destination faulted → intake `201` + row intact + execution terminal `status=error` (fault version verified as the *applied* one before submitting, clean version verified byte-for-byte after restore); scoring unavailable → `201` + `score=NULL` + `Log Manual Review` + real manual-review Telegram | **29/29**, exit 0 |

Supporting checks: `pytest -q` → **83 passed** (no test changes); `git diff
n8n/lead-intake.json` = only `options.allowedOrigins`; both Caddyfiles pass
`caddy validate` + `caddy fmt`; `systemd-analyze verify` (path-adapted unit
copies) exit 0; secret scan of tracked files clean (chat id, encryption key,
DSN password, bot-token pattern absent). Discovered and fixed during this
milestone: n8n's asynchronous publication outbox (stale-version execution
window after import) — `stack.sh start` now waits for convergence; see
[`docs/deployment.md`](deployment.md) for the ledger.

### Milestone 11 — run personally in a real browser against the live stack

Environment: the form served from `python3 -m http.server 8001 --directory
demo`, driven by **headless Chrome over the DevTools protocol** — real
`HTMLInputElement` value setting, a real click on the real Submit button,
real `fetch()` preflights, and screenshots of every state (desktop
1280×900 and a 390×844 mobile viewport). Every request below was observed
in Chrome's network log (proving the browser hit **n8n**, never FastAPI
directly), and every outcome was read from n8n's persisted execution data
plus the PostgreSQL rows afterwards. n8n `2.41.7`, FastAPI on `:8000`
(heuristic scoring except case 3), workflow unchanged from M10.

| # | Scenario (through the actual form UI) | Expected | Observed |
|---|---|---|---|
| 1 | Qualified: corporate email, long intent-rich message (pricing/enterprise/api/demo/trial) | Browser → n8n `201`, row + score, `qualified`, Telegram delivered | Network: `OPTIONS → 204`, `POST /webhook/lead-intake → 201` (only host contacted), console `[lead-form] response: 201 created`, success panel focused. exec 19: order `… Respond Created (…187) → … Log Priority Route (…271) → Build Audit Event (…286) → Build Qualified Notification (…300) → Send Qualified to Telegram (…319)`; record `score: 100`, `qualification: "qualified"`; Telegram `ok:true`, **`message_id: 11`**, text `🔥 HIGH-PRIORITY LEAD … Northwind Logistics … Score: 100 … website-contact-form`. Row #1 `score=100` |
| 2 | Not-qualified: short message + free-mail domain (source still `website-contact-form`) | `201`, success panel, not-qualified branch, **no** Telegram | Network `POST → 201`, success panel. exec 20: path `… Respond Created → … Log Normal Route → Build Audit Event`, `score: 28`, `qualification: "not_qualified"`, **0 Telegram nodes**. Row #2 `score=28` |
| 3 | Manual review: FastAPI restarted with bogus `OPENAI_API_KEY` + dead `OPENAI_BASE_URL` | `201`, success panel, `score: null`, manual-review Telegram | Network `POST → 201`, success panel. exec 24: `score: null` end-to-end (never `0`), `qualification: "manual_review"`, audit (…399) → build (…410) → send (…424); Telegram `ok:true`, **`message_id: 12`**, text `⚠️ LEAD NEEDS MANUAL REVIEW … Score: unavailable …`. Row #5 `score=NULL`. FastAPI restarted in heuristic mode afterwards |
| 4 | Duplicate: `?external_id=m11-dup-001` pinned, same content submitted twice (via the form's reset → re-fill → submit) | first `201` + success, second `409` + duplicate state, exactly one row | Network: `POST → 201` then `POST → 409`, both carrying `external_id: m11-dup-001`; console `201 created` / `409 duplicate`; duplicate panel shown. exec 23 stops at `Respond Duplicate` (5 nodes, no qualification, 0 Telegram). Exactly **one** row #3 |
| 5a | Invalid, client side: Submit clicked with all fields empty | Inline errors, no request leaves the browser | 4 field errors rendered (`name/email/company: This field is required.`, `message: Please tell us how we can help.`), focus moved to Name, **0 network requests** |
| 5b | Invalid, server side: `casey@example` (HTML5-valid, API-invalid) | Real `422`, friendly field error, no row, no Telegram | Network `POST → 422` (browser accepted what the API rejected → server stays authoritative), console `422 validation_failed`, email field error *"Please enter a valid email address."*, no success panel. exec 21 stops at `Respond Validation Failed`; **no row** (table total unaffected) |
| 6 | FastAPI stopped, valid submission | Form error state, `502`, no row, no Telegram | Network `POST → 502`, console `502 upstream_error`, banner *"We couldn't submit your request right now. Please try again."* with all values retained and the button re-enabled. exec 25 stops at `Respond Upstream Error`, **0 Telegram nodes**; **no row**. FastAPI restarted healthy afterwards |

Also personally verified: the final committed file re-run end to end after
its last edit (empty-submit block → `201` success, `outline: none` on the
programmatically focused panel, `activeElement` = success panel); mobile
390×844 render with **no horizontal overflow** (`scrollWidth ==
clientWidth == 390`), 16 px inputs (no iOS zoom), 50 px submit button;
visual review of all six states (initial form, field errors, success,
duplicate, upstream banner, mobile); JS syntax (`node --check` on the
extracted script) and tag-balance checks; form fields ↔ contract field
parity (`name`, `email`, `company`, `message`, `source` — no extra fields,
no direct-to-FastAPI call anywhere); hygiene scan of the diff (no token, no
chat id, no DSN, no machine path, no credential of any kind); `pytest -q`
→ **83 passed** (backend untouched).

### Milestone 10 — run personally against a live n8n instance

Environment: n8n `2.41.7` (nodes-base `2.41.5`) with the workflow imported
via `n8n import:workflow` + `n8n publish:workflow --id=lead-intake`, the
Telegram credential imported via `n8n import:credentials`, the n8n process
started with `TELEGRAM_CHAT_ID` set and
`N8N_BLOCK_ENV_ACCESS_IN_NODE=false`, FastAPI (uvicorn, PostgreSQL test
database) run in heuristic scoring mode — or, for case 2, with a bogus
`OPENAI_API_KEY` and a dead `OPENAI_BASE_URL`. Node order, records, errors
and the Telegram API responses were read from n8n's persisted execution data
(`execution_data`), i.e. the same artifact an operator inspects. Every
"delivered" claim below is an actual Telegram API `ok: true` response with a
`message_id` observed in the `Send … to Telegram` node output — and the
messages were visible in the destination chat.

| # | Scenario | Expected | Observed |
|---|---|---|---|
| 1 | qualified lead (intent keywords + corporate domain → score 95), threshold 70 | `201`, `qualified`/`high`, audit event, Telegram delivered with the real lead fields | exec 10: `201`, order `… Respond Created (…217) → … Log Priority Route (…334) → Build Audit Event (…349) → Build Qualified Notification (…362) → Send Qualified to Telegram (…374)`; Telegram `ok:true`, `message_id: 9`, text = `🔥 HIGH-PRIORITY LEAD … Company: Harborline Systems / Lead ID: 8 / Score: 95 / Qualification: qualified / Source: website … Action: Review and contact this lead.` |
| 2 | unscored lead (bogus `OPENAI_API_KEY` + dead `OPENAI_BASE_URL` → `score: null`) | `201`, `manual_review`, structured `score` stays `null`, Telegram delivered showing `Score: unavailable` | exec 9: `201`, `score: null`, record `event:"lead_manual_review"`, `reason:"scoring unavailable"`, audit (…212) → build (…225) → send (…240); Telegram `ok:true`, `message_id: 8`, text = `⚠️ LEAD NEEDS MANUAL REVIEW … Score: unavailable … Reason: scoring unavailable.` — structured `score` still `null` in every record |
| 3 | not-qualified lead (free-mail + newsletter + one-word message → score 12) | `201`, `not_qualified`, **no** Telegram node executes | exec 13: `201`, `qualification:"not_qualified"`, path ends `Log Normal Route → Build Audit Event`, **0 Telegram nodes executed** |
| 4 | duplicate `(source, external_id)` (case-1 payload re-sent) | `409`, no qualification/audit/notification | exec 11 (+17 re-run): `409`, nodes = `…→ Respond Duplicate` only (5 nodes) |
| 5 | payload missing `email` | `422`, no qualification/audit/notification | exec 12: `422` with FastAPI field errors passed through, `Respond Validation Failed` only |
| 6 | FastAPI process stopped | `502` (`apiStatus: null`), no qualification/audit/notification | exec 14: `502`, `Respond Upstream Error` only, **0 Telegram nodes** |
| 7 | **Telegram fault**: `Send Qualified to Telegram`'s `chatId` replaced with an invalid literal destination (credential untouched), then a qualified lead | genuine `201` **and** a visible execution failure | exec 16: caller `201` with `lead_id: 12` stored; execution `status=error`; `NodeApiError: Bad request … Bad Request: chat not found` on `Send Qualified to Telegram` with `chatId: 'INVALID_FAULT_DESTINATION_7'` persisted; `Build Audit Event` had already run (…834 → build …846 → send …857); no `onError` on the node, failure not swallowed. (Execution 15, 18 s after the reload, ran the previous published version — see the operational caveat in the M10 section.) |

Also personally verified for this milestone: **audit-before-notification**
from persisted start timestamps in both the success (exec 10) and the fault
(exec 16) runs; actual Telegram deliveries for both message types (`message_id`
8 = manual review, 9 = qualified — plus earlier debugging deliveries 5, 6, 7
from pre-final iterations and 10 from the execution-15 race); the structural
validator over the JSON (**19 nodes**, unique ids/names, all connections
resolve, no orphans, exactly the 7 intended terminals, every M7–M9 node
byte-identical to `HEAD` except the two appended connection arrays, Telegram
`typeVersion` `1.2 ∈ [1, 1.1, 1.2]` as declared in the installed sources,
credential reference = `{id, name}` only, notification policy wiring —
`not_qualified` cannot reach any Telegram node, `audit y < build y` for both
chains — and hygiene regexes clean: no token pattern, no chat id, no
machine paths, no DSNs, no SQL); message-format unit tests of the build-node
code (11/11: exact qualified/manual formats, `score` stays `null`, numeric
score rendered when present, HTML-escaping of `&<>`, loud failure on
missing/blocked `TELEGRAM_CHAT_ID`); `pytest -q` → **83 passed** (backend
untouched).

### Milestone 9 — run personally against a live n8n instance

Environment: n8n `2.41.7` under `/tmp`, workflow re-imported with
`n8n import:workflow` + `n8n publish:workflow --id=lead-intake` and the
service restarted between variants, FastAPI (uvicorn, PostgreSQL 16 test
database) run either in heuristic mode or — for case 3 — with a bogus
`OPENAI_API_KEY` and a dead `OPENAI_BASE_URL`. Executed-node lists, node
start order, records and errors were read from n8n's persisted execution
data (`execution_data`), i.e. the same artifact an operator inspects, never
from the webhook response (which does not carry the routing result).

| # | Scenario | Expected | Observed |
|---|---|---|---|
| 1 | qualified lead (intent keywords + corporate domain → score 95), default threshold 70 | `201`, `qualified`/`high`, record + summary, audit event | exec 27: `event:"lead_qualified"`, `priority:"high"`, `summary:"HIGH PRIORITY LEAD\nCompany: Navy Yard Corp\nLead ID: 3\nScore: 95\n…"`, audit `timestamp:"2026-10-05T10:42:09…"`, `executionId:"27"`, `executionMode:"production"` |
| 2 | not-qualified lead (free-mail + newsletter + one-word message → score 12) | `201`, `not_qualified`/`normal` | exec 28: `event:"lead_not_qualified"`, `priority:"normal"`, `summary:"NOT QUALIFIED\n…"` |
| 3 | unscored lead: FastAPI restarted with bogus `OPENAI_API_KEY` + dead `OPENAI_BASE_URL` → M4 rule returns `score: null` | `201` with `score: null`, `manual_review`, no fabricated score | exec 32: `"score":null`, `event:"lead_manual_review"`, `priority:null`, `reason:"scoring unavailable"`, `summary:"MANUAL REVIEW REQUIRED\n…\nScore: null\n…\nReason: scoring unavailable"` |
| 4 | duplicate `(source, external_id)` | `409`, **no** operational node runs | exec 29: `409`, nodes = …`Respond Duplicate` only |
| 5 | payload missing `email` | `422`, **no** operational node runs | exec 30: `422`, `Respond Validation Failed` only |
| 6 | FastAPI process stopped | `502` (`apiStatus: null`), no qualified/not-qualified event | exec 31: `502`, `Respond Upstream Error` only |
| 7 | threshold flip: n8n restarted with `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` + `LEAD_QUALIFIED_THRESHOLD=95`, **same score-75 payload** as case 8 | `not_qualified` (75 < 95), `thresholdSource: "environment"` | exec 34: `qualification:"not_qualified"`, `qualifiedThreshold:95`, `thresholdSource:"environment"`, `event:"lead_not_qualified"` |
| 8 | boundary lead at default threshold (score 75 ≥ 70) | `qualified` | exec 33: `Log Priority Route`, `qualifiedThreshold:70`, `thresholdSource:"workflow-default (n8n blocks $env access)"` |
| 9 | clean run after restoring the committed workflow | `201`, `status=success`, full chain | exec 39: `201`, order = `… Route on API Status → Respond Created → Extract Lead + Score → … → Build Audit Event` |
| 10 | **fault, pre-M9 order**: unconditional `throw` in `Extract Lead + Score` with `Respond Created` moved back to `[960,-240]` | documents the failure mode M9 fixes | exec 35: caller got `500 {"message":"Error in workflow"}`, execution `status=error` with the injected message persisted, `Respond Created` never ran — **and the row was still created** (lead 9): a fake API failure with an accepted lead |
| 11 | **fault, early operational node**: same `throw`, committed workflow (respond above extract) | caller keeps `201`; failure visible; lead accepted | exec 36: `201` envelope returned, execution `status=error` (`M9 fault injection: operational node failure [line 1]`), `Respond Created` ran first, row exists (lead 10) |
| 12 | **fault in the new audit node**: `throw` in `Build Audit Event` | caller keeps `201`; branch record still persisted; failure visible | exec 38: `201`, `Log Priority Route` record present (`leadId:12`, `event:"lead_qualified"`, full summary), execution `status=error` at `Build Audit Event`, row exists (lead 12) |

Executions 25–26 (the first M9 runs, before the position fix) are what
exposed the ordering problem: they show `Respond Created` executing *after*
`Build Audit Event`. From case 9 onward every successful run shows the
caller answered first.

Also verified for this milestone: the structural validator over the JSON
(15 nodes with unique ids/names, connection integrity, reachability from the
single trigger, only the intended terminals — the three branch nodes now
feed `Build Audit Event`, which is terminal; **every M7/M8 node and
connection byte-identical to `HEAD`** except the three rewritten branch
records and `Respond Created`'s canvas position; the `Respond Created` above
`Extract Lead + Score` invariant; threshold/base-URL config still defined in
exactly one node; no scoring logic, no SQL, no secrets or absolute paths;
node `typeVersion`s present in the installed `n8n-nodes-base` sources:
webhook `2.1`, code `2`, httpRequest `4.5`, switch `3.4`, respondToWebhook
`1.5`); `pytest` → **83 passed** (backend untouched).

Environment quirk hit while testing: n8n answers `/healthz` before its
published webhooks are registered, so the very first request after a restart
can return `404 Cannot POST /webhook/lead-intake`; it succeeded on retry a
few seconds later. Not a workflow defect — the reload script now waits
before handing over.

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
(qualification runs on a parallel fan-out, so the envelope never depends on
it). **Correction from Milestone 9:** the sentence that used to follow here
— "the 201 branch still runs `Respond Created` first" — was wrong. Measured
from the persisted node start times, `Respond Created` ran *last* in these
executions; see [Caller-response isolation](#caller-response-isolation) for
what Milestone 9 did about it. (The `action`-shaped records in the table
above were themselves superseded in Milestone 9 by the `event`/`summary`
records — the current shape is documented earlier in this file.)

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

**Not tested (any milestone):** running n8n in queue/multi-main mode,
Dockerised n8n, `webhook-test` (editor) URLs, manual "test" executions
(so every observed audit event carries `executionMode: "production"`),
authentication on the lead API (none exists — the only credential in play is
n8n's stored Telegram token), concurrent submissions to the same
`(source, external_id)`, delivery to any channel other than the
single-destination Telegram send of Milestone 10 (no email/Slack/CRM
integration exists here and none is faked), and anything beyond the Bot API
responses persisted in the execution data (no Telegram delivery/read receipts
are tracked). The backend contract itself is covered by the 83 pytest tests.

**Known limitations:** the threshold is an unvalidated business default
(above); the `not_qualified` record still exists only in n8n's execution
data — only `qualified` and `manual_review` are pushed to a human, and that
Telegram send is a single attempt with no retry, no queue and no
delivery/read-receipt tracking beyond the Bot API response persisted in the
execution data; exactly one configured destination, no recipient rules;
`timestamp` is n8n's wall clock at audit-node execution, not FastAPI's
`created_at` (FastAPI's value stays on the lead row itself); a *failed*
operational or notification node surfaces as a failed execution
(`status: error`) while the caller keeps its `201` — verified in cases 11–12
(M9) and case 16 (M10) — so such failures are noticed only by monitoring of
n8n, never by the caller.
