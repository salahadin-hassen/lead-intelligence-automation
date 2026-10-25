# LeadIntel — AI Lead Insights (OpenAI)

Optional, supplementary AI enrichment: for one lead, the OpenAI API produces a short **summary**, an **intent category**, an **urgency** level, one **recommended next action**, and a **reason**. A sales rep can read a lead and know what to do next without opening the full message.

The insights never replace, adjust or feed the deterministic score (`score`, `score_reason`). They make no business decisions: a human decides. The feature is **off by default**.

---

## 1. Architecture & Data Flow

```
/demo page (demo/lead-form.html)          any other caller
  POST /webhook/lead-intake                 POST /webhook/lead-intake
  {…, "demo_insights": true}                {…}                       (Caddy / Cloudflare Worker → n8n)
   │                                          │
   ▼                                          ▼
n8n  lead-intake (n8n/lead-intake.json)
   Prepare Lead Payload → Create Lead in FastAPI → Route on API Status
     ├─ 409 / 422 / other → unchanged responses, no AI call
     └─ 201 ─┬─ normal caller: Respond Created (unchanged envelope, sent FIRST)
             └─ Generate AI Insights   POST {API}/leads/{lead_id of THIS create}/insights (one attempt)
                  ▼
                Attach AI Insights     lead-id check, fail-open status (n8n/attach-ai-insights.js)
                  ├─ demo caller: Respond Created with AI Insights (same envelope + ai_insights)
                  ▼
                Extract Lead + Score → Qualification → Telegram / WhatsApp / audit   (unchanged)
   ▼
FastAPI  app/routes/leads.py + app/insights.py
   │  - returns stored insights if they already exist (no model call)
   │  - atomic claim (ai_status='pending') → at most one paid call per lead
   │  - one OpenAI Responses API call, strict JSON schema, no retries
   │  - validates the reply (app/models.py:AiInsights) before storing it
   ▼
PostgreSQL  leads.ai_status / ai_insights / ai_model / ai_error / ai_usage / ai_updated_at
            (sql/005_add_lead_ai_insights.sql)
```

- `POST /leads` is **unchanged** and never calls OpenAI. The deterministic score is computed and stored before any AI request and is never modified by it.
- **Normal intake:** every created lead is enriched once, after the caller has already received the unchanged response. Insights are stored and readable via `GET /leads/{id}/insights`. They are not added to the public response and do not change qualification or notifications.
- **Demo experience:** the `/demo` page (the only place the form exists; it tells visitors to use synthetic data) sends `"demo_insights": true`. That request is answered after enrichment and includes `ai_insights` for **that lead only**. `Prepare Lead Payload` drops the flag, so FastAPI never sees it.
- **Lead-ID safeguard:** the insights URL uses the `lead_id` from this execution's `Create Lead in FastAPI` response. `Attach AI Insights` accepts the reply only if its `lead_id` is the same number; otherwise it returns `status: "unavailable", error: "lead_id_mismatch"` and never another lead's data. The page checks `ai_insights.lead_id === lead.lead_id` again before rendering. Regression tests cover the earlier "created lead 23, shown lead 21" bug.
- The OpenAI key lives only on the FastAPI service (`OPENAI_API_KEY`). n8n reaches the endpoint with its existing FastAPI Header Auth credential. No OpenAI key exists in n8n, workflow JSON or the browser.
- **Paid-call ceiling:** every paid call must first reserve one unit of a global hourly/daily budget in the same database transaction that claims the lead (Section 11). This holds for every caller and path, including direct webhook requests that bypass the website.
- **Do not set `LLM_API_KEY`** to enable this feature. That older setting makes an LLM *replace* the deterministic score (`app/scoring.py`).
- `n8n/lead-intake-ai-demo.json` (separate demo webhook path, no notifications) remains available and uses the same `Attach AI Insights` safeguard.

### Endpoints (both require the existing API bearer token)

| Endpoint | Calls OpenAI? | Behaviour |
| :--- | :--- | :--- |
| `POST /leads/{id}/insights` | At most once per lead | Returns stored insights if `completed`; otherwise claims, calls, validates, stores. `?retry=true` re-attempts a **failed** lead (never a completed one). |
| `GET /leads/{id}/insights` | Never | Returns the stored state (`not_requested`, `pending`, `completed`, `failed`). |

Every handled outcome answers `200` with a `status`; an unknown lead is `404`.

### What is sent to OpenAI
Only `company`, `source` and the lead `message`, truncated to `AI_INSIGHTS_MAX_INPUT_CHARS` (2,000) characters. The **name and email address are never sent**. The route does not even load them. Requests use `"store": false`.

### Untrusted input
The message is wrapped in `<lead>…</lead>` tags (tags inside the text are stripped) and the instructions tell the model to treat it as data and ignore embedded instructions. Strict Structured Outputs plus server-side validation means a manipulated reply can only ever be one of the five categories and three urgency levels with capped text. The model has no tools and triggers no actions.

---

## 2. Example (synthetic)

Lead (`POST /leads`):
```json
{"name": "Synthetic Prospect", "email": "synthetic.prospect@example.com",
 "company": "Example Logistics", "source": "website-contact-form",
 "external_id": "ai-demo-0001",
 "message": "Please send a quote for 20 sales reps; we are evaluating tools this quarter."}
```

`POST /leads/{id}/insights` (illustrative shape; real wording comes from the model):
```json
{
  "lead_id": 42,
  "status": "completed",
  "insights": {
    "summary": "Asks for a quote to automate lead qualification for 20 sales reps.",
    "intent_category": "quote_request",
    "urgency": "medium",
    "recommended_next_action": "Send pricing for 20 seats and offer a 30-minute call this week.",
    "reason": "Explicit request for pricing with an evaluation this quarter."
  },
  "model": "gpt-6-luna",
  "error": null,
  "usage": {"input_tokens": 352, "cached_tokens": 0, "output_tokens": 94, "reasoning_tokens": 0, "total_tokens": 446},
  "updated_at": "2026-10-24T14:00:00Z",
  "cached": false
}
```

The `/demo` request's `201` response (and the separate demo workflow's) is the normal website envelope (`outcome`, `lead`, `score`, `score_reason`) plus a sanitized `ai_insights` object built by `Attach AI Insights`: `{"status": "completed", "lead_id": 42, "insights": {…the five fields…}, "model": "gpt-6-luna", "error": null}`, or a non-completed status such as `{"status": "unavailable", "lead_id": 42, "insights": null, "error": "insights_http_503"}`. Usage and cache metadata are never included in public responses.

---

## 3. Failure Handling

Lead intake never depends on AI: the lead is already stored and scored before insights are requested.

| Situation | `status` / `error` | Model calls |
| :--- | :--- | :--- |
| `AI_INSIGHTS_ENABLED=false` | `disabled` | 0 |
| Enabled, `OPENAI_API_KEY` empty | `disabled` / `not_configured` | 0 |
| Insights already completed | `completed`, `cached: true` | 0 |
| Another request is in progress | `pending` | 0 |
| Hourly or daily call budget reached (Section 11) | `failed` / `budget_exceeded` (not stored; the lead stays eligible) | 0 |
| Budget check itself fails (database error) | HTTP 500 from the API (n8n: `unavailable`), no call | 0 |
| Timeout (`AI_INSIGHTS_TIMEOUT_SECONDS`, 15 s) | `failed` / `timeout` | 1 |
| 401/403 | `failed` / `auth_error` | 1 |
| 429 rate limit | `failed` / `rate_limited` | 1 |
| 429 credits/spend exhausted (`credit_balance_exhausted`, `insufficient_quota`, spend limits) | `failed` / `insufficient_quota` | 1 |
| 5xx / network error | `failed` / `provider_error` | 1 |
| Other 4xx (e.g. unsupported parameter) | `failed` / `request_error` | 1 |
| Output hit `max_output_tokens` | `failed` / `truncated` | 1 |
| Refusal | `failed` / `refused` | 1 |
| Not JSON, missing/extra field, unknown category/urgency, duplicate keys | `failed` / `invalid_output` | 1 |

There are no automatic retries and no "repair" calls. A failed lead is only re-attempted with `?retry=true`. Results are never fabricated: `insights` is `null` unless a reply passed validation. Free-text fields are capped (summary 400, action 300, reason 300 characters) because strict mode cannot express `maxLength`. A `pending` claim older than timeout + 60 s (process died mid-call) can be taken again.

---

## 4. Model, Pricing & Cost per Lead

**Default model:** `gpt-6-luna`, with `reasoning.effort` set to `none` so no paid reasoning tokens are generated. All of these are configurable via `AI_INSIGHTS_MODEL` / `AI_INSIGHTS_REASONING_EFFORT`.

Checked on 2026-10-24 against the official pages:
- [Models](https://developers.openai.com/api/docs/models) recommends GPT-6 Luna as "our most efficient model for focused, high-volume tasks".
- [GPT-6 Luna model page](https://developers.openai.com/api/docs/models/gpt-6-luna): Structured Outputs and the Responses API supported; reasoning efforts `none` … `max` (default `medium`).
- [Pricing](https://developers.openai.com/api/docs/pricing), Standard tier: **$0.10 / 1M input tokens, $0.50 / 1M output tokens** (prompts above 272K tokens cost more; not reachable here).
- Lower-cost alternatives still listed: `gpt-5-nano` ($0.05 / $0.40). `gpt-5.4-nano` ($0.20 / $1.25) is marked *Deprecated* on its model page. If you switch models, also check which `reasoning.effort` values it accepts; an unsupported value fails as `request_error`.

**Measured:** one live local smoke test on 2026-10-24 (synthetic ~110-character quote request, `gpt-6-luna`, reasoning `none`) reported **352 input tokens (0 cached), 94 output tokens (0 reasoning)**, ≈ $0.000035 + $0.000047 ≈ **$0.00008** at the Standard prices above. Billed amounts are authoritative in the OpenAI dashboard. Every call's reported usage is stored in `ai_usage` and returned as `usage`.

**Estimate** (conservative, ~4 characters per token from the request built by `app/insights.py`):

| Case | Input tokens | Output tokens | Cost per lead |
| :--- | ---: | ---: | ---: |
| Typical message (~120 chars) | ~550 | ~100 | ≈ $0.000055 + $0.000050 ≈ **$0.0001** |
| Worst case (2,000-char cap, full 300-token output) | ~1,000 | 300 | ≈ $0.00010 + $0.00015 ≈ **$0.00025** |

$5 of credit covers roughly **20,000 leads in the worst case** and about **45,000 typical leads**. Prices change: re-check the pricing page before relying on these numbers. Set a project budget and limit in the OpenAI dashboard as a hard stop.

---

## 5. Configuration (no secrets in this document)

FastAPI service (`leadintel-api`) environment:

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `AI_INSIGHTS_ENABLED` | `false` | Master switch; `false` means no model calls |
| `OPENAI_API_KEY` | empty | OpenAI project key (secret) |
| `AI_INSIGHTS_MODEL` | `gpt-6-luna` | Model ID |
| `AI_INSIGHTS_REASONING_EFFORT` | `none` | Empty string omits the parameter |
| `AI_INSIGHTS_TIMEOUT_SECONDS` | `15` | One attempt, then `timeout` |
| `AI_INSIGHTS_MAX_OUTPUT_TOKENS` | `300` | Output budget per call |
| `AI_INSIGHTS_MAX_INPUT_CHARS` | `2000` | Message truncation before the call |
| `AI_INSIGHTS_MAX_CALLS_PER_HOUR` | `20` | Global cap on paid calls per clock hour (all callers together); `0` = no calls |
| `AI_INSIGHTS_MAX_CALLS_PER_DAY` | `100` | Global cap on paid calls per day; `0` = no calls |

### Creating and storing the key (you do this; never paste it into chat or files)
1. platform.openai.com → **Projects** → create a project (e.g. `leadintel`) → **Limits**: set a monthly budget (e.g. $5) so the balance cannot be exceeded.
2. **API keys** → create a secret key for that project with restricted permissions: **Responses**/model request write only, if offered.
3. Render → `leadintel-api` → **Environment** → add `OPENAI_API_KEY` (paste the key there only) and `AI_INSIGHTS_ENABLED=true`. Saving restarts the API.
4. To disable: set `AI_INSIGHTS_ENABLED=false` (or delete the key). Intake keeps working unchanged.

Deploying this code applies `sql/005_add_lead_ai_insights.sql` automatically at API startup. It's additive: nullable columns plus a check constraint, idempotent, and existing rows stay `NULL`.

### n8n credentials (no OpenAI credential in n8n)
`Generate AI Insights` uses the **same** FastAPI Header Auth credential as `Create Lead in FastAPI`. Nothing new is stored in n8n.

---

## 6. What the Website Shows (`/demo`)

After a successful demo submission the result panel shows the existing qualification score and reason, plus an **AI lead insights** card:
- `completed`: summary, intent, urgency, recommended next action and "why", labelled *Generated for lead #N by <model>*, with the note that these are AI-generated suggestions and that the score is calculated separately.
- `disabled`, `failed` (timeout, credits exhausted, rate limited, invalid output, …), `pending`, `unavailable` (lead-ID mismatch, HTTP/network failure) or no `ai_insights` at all: an explicit status badge and a plain explanation that the lead was still saved and scored. No fields are shown.

The card is hidden for duplicates (409), validation errors, upstream errors and network failures. All AI text is set with `textContent` (never HTML). The submit button stays disabled while a request is running. Normal (non-demo) callers never receive insights.

---

## 7. Failure Handling in the Workflow (fail-open)

The lead is created and scored **before** enrichment, and nothing after `Create Lead in FastAPI` can cause a second create or a second paid call.

| Situation | Caller receives | `ai_insights` (demo) | Notifications |
| :--- | :--- | :--- | :--- |
| AI disabled on the API | `201` | `status: "disabled"` | unchanged |
| Global call budget reached | `201` | `status: "failed"`, `error: "budget_exceeded"` | unchanged |
| OpenAI timeout / 429 rate limit / credits exhausted / invalid output | `201` | `status: "failed"`, `error`: `timeout` / `rate_limited` / `insufficient_quota` / `invalid_output` | unchanged |
| Insights endpoint HTTP error (401, 404, 5xx) | `201` | `status: "unavailable"`, `error: "insights_http_<code>"` | unchanged |
| n8n HTTP node error (timeout, connection refused) | `201` | `status: "unavailable"`, `error: "insights_request_failed"` | unchanged |
| Reply names another lead | `201` | `status: "unavailable"`, `error: "lead_id_mismatch"` | unchanged |
| Unexpected status or invalid fields | `201` | `status: "unavailable"`, `error: "unexpected_insights_status"` / `"invalid_insights"` | unchanged |
| Duplicate (`409`), validation (`422`), upstream error | unchanged responses | not requested | unchanged |

`Generate AI Insights` has `retryOnFail: false`. A failed lead is only re-attempted manually with `POST /leads/{id}/insights?retry=true`. Public responses never include usage, cache flags, credentials or stack traces.

---

## 8. Production Rollout (manual; nothing here has been applied yet)

The Docker entrypoint **does not** re-import `lead-intake` on deploy: it only imports it if missing. **Never set `N8N_FORCE_IMPORT_WORKFLOW=true` for this change.** It would replace the live workflow with the repository copy and reset its credential bindings. The live workflow may also differ from the repository copy, so it must be updated by hand.

0. **Deploy the API first** (`leadintel-api`, this commit). It applies `sql/006_add_ai_insights_budget.sql` on startup, and the default caps (20/hour, 100/day) protect immediately with no Render change. Before that deploy, **confirm `lead-intake-ai-demo` is inactive** in n8n: while it's active on the current API it is an uncapped public AI trigger. In OpenAI, keep a project budget/limit as a hard backstop.
1. **Back up.** In n8n, open `lead-intake` → **⋯ → Download** (keep the JSON) and **⋯ → Duplicate** (keep the copy inactive). The previous repository version is also in Git: `git show 97656c7:n8n/lead-intake.json`.
2. **Compare.** Confirm the live `Route on API Status` → `created` output connects to `Respond Created` and `Extract Lead + Score` (the state this change starts from). If it differs, stop and reconcile first.
3. **Copy the five new nodes.** Import `n8n/lead-intake.json` as a **new, separate** workflow (Workflows → Import from File; do not open it over the live one). Then select `Demo Insights Requested?`, `Generate AI Insights`, `Attach AI Insights`, `Return AI Insights?` and `Respond Created with AI Insights`, copy them, paste them into the live `lead-intake`, and delete the imported copy without activating it.
4. **Rewire the live workflow** (only this output changes):

   | From | To |
   | :--- | :--- |
   | `Route on API Status` → `created` | `Demo Insights Requested?` **and** `Generate AI Insights` (remove the old links to `Respond Created` and `Extract Lead + Score`) |
   | `Demo Insights Requested?` → `Fallback` | `Respond Created` (leave `demo` unconnected) |
   | `Generate AI Insights` | `Attach AI Insights` |
   | `Attach AI Insights` | `Return AI Insights?` **and** `Extract Lead + Score` |
   | `Return AI Insights?` → `demo` | `Respond Created with AI Insights` |

5. **Keep the execution order.** On the canvas, place `Demo Insights Requested?` **above** `Generate AI Insights`, and `Return AI Insights?` **above** `Extract Lead + Score` (n8n v1 runs the top-most sibling first).
6. **Credential.** `Generate AI Insights` → the same FastAPI Header Auth credential as `Create Lead in FastAPI`. Leave every other node and credential untouched. Save.
7. **Deploy the website.** The new `/demo` card ships with `demo/lead-form.html`: redeploy `leadintel-caddy` and/or rebuild the Cloudflare Pages assets (`deploy/bin/build-pages.sh`). Order doesn't matter. An old page ignores `ai_insights`; the new page shows "Not returned" until the workflow is updated.

### Smoke test (one synthetic lead, fresh external ID)
1. Open `/demo`, use a preset or fictional data, submit once. A new `external_id` is generated automatically; note it from the result panel.
2. Expect: `201`, the score card, and the AI card with status *Generated* (or an explicit unavailable status).
3. **Confirm the insights belong to the created lead:** in the browser DevTools response (or the n8n execution of `Attach AI Insights`), `lead.lead_id` must equal `ai_insights.lead_id`, and the summary must describe the message you sent.
4. Press **Re-submit (Test Duplicate)**: expect `409`, the AI card hidden, and no new `Generate AI Insights` node in the execution (no model call).
5. In n8n Executions, confirm one `Generate AI Insights` call for the created lead, and that qualified/manual-review leads still reach their Telegram/WhatsApp nodes as before.

### Rollback
- **Disable AI only:** set `AI_INSIGHTS_ENABLED=false` on `leadintel-api`. Intake continues; the demo shows "Disabled".
- **Restore the workflow:** in the live `lead-intake`, reconnect `Route on API Status` → `created` to `Respond Created` and `Extract Lead + Score` and delete the five nodes, or restore the JSON downloaded in step 1. The `/demo` page then shows "Not returned" and keeps working.

---

## 9. Test Evidence

- **Automated:** `tests/test_ai_insights.py` and `tests/test_ai_intake_integration.py` (mocked HTTP; no OpenAI calls).
- **Local end-to-end (2026-10-25):** real n8n 2.41.7 running the new `n8n/lead-intake.json`, local FastAPI on an isolated database, and a fake local OpenAI endpoint. Results:
  - demo path: `201`, `ai_insights.completed`, `lead_id` 1 = 1;
  - normal path: unchanged `201` without `ai_insights`, enriched afterwards;
  - duplicate: `409`, 0 calls;
  - quota: `failed/insufficient_quota`; timeout: `failed/timeout`; invalid output: `failed/invalid_output`; AI disabled: `disabled`;
  - qualified lead: `201` with insights, then the existing priority/notification branch.

  Recorded node order: normal `Respond Created → Generate AI Insights → Attach AI Insights → Extract Lead + Score`; demo `Generate → Attach → Respond Created with AI Insights → Extract`.
- **Live (reported by the operator, separate demo workflow):** lead `24` returned `201` with `ai_insights` for lead `24`: `completed`, `quote_request`, `medium`, model `gpt-6-luna`, deterministic score `65`. An earlier run of that workflow showed insights for lead 21 while creating lead 23; the lead-ID safeguard above prevents this.
- **Abuse protection (local, 2026-10-25):** `tests/test_ai_abuse_protection.py`, including real-thread concurrency against Postgres (12 concurrent reservations, cap 5 → exactly 5; 6 for one lead → 1). The same suite fails against a no-budget, a no-rollback and a racy implementation. Real n8n 2.41.7 sandbox with hourly cap 2 and a fake OpenAI endpoint:
  - direct POST without the flag: enriched (counted);
  - demo request: `completed`;
  - the next demo, a tampered-flag request and a request injecting `lead_id`/`ai_insights`: `201` with `budget_exceeded` or no insights, and only their **own** lead id;
  - duplicate: `409`.

  The fake endpoint received exactly 2 calls.
- **Not yet verified:** the updated live `lead-intake` workflow and the updated `/demo` page in production (Section 8).

---

## 10. Known Limitations

- Insights are model output: plausible but possibly wrong. They are shown as suggestions and are not used for routing, scoring or notifications.
- The demo opt-in (`demo_insights: true`) is a client-side flag, not authorization. It only selects which response is returned. It can't add, skip or bypass a paid call, and it only ever returns the caller's **own** lead. The cost control is the server-side budget (Section 11).
- The budget is global, so a determined caller can use up the hourly/daily allowance. AI insights then pause for everyone (`budget_exceeded`) until the window resets, while lead intake, scores and notifications continue. The **number of paid calls** cannot exceed the cap; dollar amounts are estimates (Section 11).
- Demo responses wait for enrichment (typically a few seconds; bounded by FastAPI's 15 s OpenAI timeout and n8n's 30 s node timeout).
- `GET /leads`, `GET /leads/{id}`, the CSV export and Telegram/WhatsApp messages do not include insights (unchanged contracts).
- `usage` is OpenAI's reported token count per call (stored in `ai_usage`), not a billed amount; the OpenAI dashboard is authoritative for cost.
- Prompt-injection resistance is best-effort; the impact is bounded to a wrong-but-valid category, urgency or text.

---

## 11. Abuse & Cost Protection

### Public request path
```
Browser (/demo) ─► Cloudflare Worker (/webhook/lead-intake) ─► Caddy ─┐
Browser (/demo) ─► Caddy (leadintel-caddy.onrender.com/webhook/*) ────┼─► n8n lead-intake ─► FastAPI POST /leads
Anyone ─────────► n8n directly (leadintel-n8n.onrender.com/webhook/lead-intake) ─┘      └─► POST /leads/{id}/insights ─► OpenAI
```
n8n is a public Render web service, so the website and its proxies can be bypassed entirely. Every 201 (any caller, any flag) runs `Generate AI Insights`. Without a control, the cost would be one paid call per request with a fresh `external_id`.

### What does **not** protect against this
- `demo_insights`: client-supplied, and enrichment runs without it anyway.
- Duplicate detection: a new `external_id` is a new lead.
- One-call-per-lead claim and token/input caps: they bound each call, not the number of calls.
- An OpenAI billing alert or budget: a useful backstop, but it stops AI for everyone and is configured outside this code.
- Per-IP limits: **not used, because no trustworthy client IP reaches n8n or the API.** The Cloudflare Worker forwards only `Content-Type`/`Accept`. Caddy ignores incoming `X-Forwarded-*` from untrusted peers (no `trusted_proxies` is configured) and records Render's edge as the peer. Direct-to-n8n requests carry whatever `X-Forwarded-For` the caller sends. An IP limit would be spoofable or would put every visitor in one bucket.

### The control
`POST /leads/{id}/insights` is the **only** code path in the insights feature that spends OpenAI money (the legacy LLM scoring path is off by default; see Tuning and remaining risks). Before the model call, `app/db.py:reserve_lead_insights` runs one transaction that:
1. claims the lead (`ai_status = 'pending'`, as before);
2. increments the current **hour** window in `ai_insights_budget` only if it is below `AI_INSIGHTS_MAX_CALLS_PER_HOUR`;
3. does the same for the current **day** window.

If any step fails, everything rolls back: no claim, no counted window, no call. The conditional `INSERT … ON CONFLICT DO UPDATE … WHERE calls < limit` row-locks the window, so concurrent requests cannot overshoot. Completed, pending, failed-without-retry, disabled and duplicate paths never consume budget.

### Behaviour
- **Budget reached:** the API answers `status: "failed", error: "budget_exceeded"` without calling OpenAI and without storing anything, so the lead stays eligible (`POST /leads/{id}/insights` later enriches it). The workflow still returns `201`; the `/demo` page says the demo's AI allowance is used up. It also logs `AI insights call budget reached`.
- **Protection unavailable** (budget table/database error): the exception propagates (HTTP 500), so the model is **never** called. n8n maps it to `unavailable`, and lead creation is unaffected because it already happened.
- **What is guaranteed:** at most `AI_INSIGHTS_MAX_CALLS_PER_HOUR` calls per clock hour and `AI_INSIGHTS_MAX_CALLS_PER_DAY` per calendar day (in the database time zone).
- **Estimated cost with defaults (an estimate, not a guaranteed dollar ceiling):** 100 calls/day × ≈ $0.00025 ≈ **$0.025/day** (about $0.75/month). This assumes the Standard prices checked on 2026-10-24 ($0.10 / $0.50 per 1M tokens), ~1,000 input tokens (2,000-character message cap plus instructions and schema, estimated), the 300-token output cap and `reasoning.effort: none`. One measured real call cost ≈ $0.00008. Price changes, a different `AI_INSIGHTS_MODEL` or reasoning effort, or OpenAI billing a request that timed out on our side all change the actual amount. Keep an OpenAI project budget as the dollar backstop.
- Fixed windows can allow up to 2× the hourly cap across an hour boundary; the daily cap still holds.

### Tuning and remaining risks
- Raise or lower the caps with `AI_INSIGHTS_MAX_CALLS_PER_HOUR` / `AI_INSIGHTS_MAX_CALLS_PER_DAY` on `leadintel-api`. `0` stops all calls without disabling the feature flag.
- **Availability vs. cost:** the cap bounds the number of paid calls, not fairness between callers. If abuse becomes a real problem, add a verified per-client signal, e.g. a free Cloudflare Turnstile challenge verified server-side, or a verified edge IP after configuring and testing `trusted_proxies`.
- **Legacy LLM scoring is not budgeted:** `app/scoring.py` calls an LLM on every `POST /leads` when `LLM_API_KEY` is set. It can point at OpenAI with `LLM_PROVIDER=openai-compatible`, and it replaces the deterministic score. It is off by default and must stay off: keep `LLM_API_KEY` unset on `leadintel-api`.
- **Pre-existing, outside AI:** the public webhook itself has no rate limit. A caller can create leads in bulk, and high-intent spam reaches the existing Telegram/WhatsApp notifications. WhatsApp business messages may be billed by Meta. This change does not alter that behaviour.
