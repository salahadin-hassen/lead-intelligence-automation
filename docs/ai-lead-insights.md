# LeadIntel — AI Lead Insights (OpenAI)

Optional, supplementary AI enrichment: for one lead, the OpenAI API produces a short **summary**, an **intent category**, an **urgency** level, one **recommended next action**, and a **reason**. A sales rep can read a lead and know what to do next without opening the full message.

The insights never replace, adjust or feed the deterministic score (`score`, `score_reason`). They make no business decisions: a human decides. The feature is **off by default**.

---

## 1. Architecture & Data Flow

```
Website / API client
   │ POST /webhook/lead-intake-ai-demo   (demo workflow, inactive by default)
   ▼
n8n  Lead Intake + AI Insights (demo)  ── n8n/lead-intake-ai-demo.json
   │ 1. POST {API}/leads                  (unchanged: validation, dedup, deterministic score)
   │ 2. on 201: POST {API}/leads/{id}/insights
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

- `POST /leads` is **unchanged** and never calls OpenAI. The production `lead-intake` workflow is untouched.
- The OpenAI key lives only on the FastAPI service (`OPENAI_API_KEY`), the same server-side pattern already used for `LLM_API_KEY`. n8n reaches the endpoint with its existing FastAPI Header Auth credential. No OpenAI key exists in n8n or in any workflow JSON. This keeps validation, the one-call guarantee and persistence in one place that the test suite covers.
- **Do not set `LLM_API_KEY`** to enable this feature. That older setting makes an LLM *replace* the deterministic score (`app/scoring.py`).

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

The demo workflow's `201` response is the normal website response (`outcome`, `lead`, `score`, `score_reason`) plus `ai_insights` (the object above, or `{"status": "unavailable", "error": "insights_http_503"}` if the insights call itself failed).

---

## 3. Failure Handling

Lead intake never depends on AI: the lead is already stored and scored before insights are requested.

| Situation | `status` / `error` | Model calls |
| :--- | :--- | :--- |
| `AI_INSIGHTS_ENABLED=false` | `disabled` | 0 |
| Enabled, `OPENAI_API_KEY` empty | `disabled` / `not_configured` | 0 |
| Insights already completed | `completed`, `cached: true` | 0 |
| Another request is in progress | `pending` | 0 |
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

### Creating and storing the key (you do this; never paste it into chat or files)
1. platform.openai.com → **Projects** → create a project (e.g. `leadintel`) → **Limits**: set a monthly budget (e.g. $5) so the balance cannot be exceeded.
2. **API keys** → create a secret key for that project with restricted permissions: **Responses**/model request write only, if offered.
3. Render → `leadintel-api` → **Environment** → add `OPENAI_API_KEY` (paste the key there only) and `AI_INSIGHTS_ENABLED=true`. Saving restarts the API.
4. To disable: set `AI_INSIGHTS_ENABLED=false` (or delete the key). Intake keeps working unchanged.

Deploying this code applies `sql/005_add_lead_ai_insights.sql` automatically at API startup. It's additive: nullable columns plus a check constraint, idempotent, and existing rows stay `NULL`.

### n8n demo workflow (credential binding only; no OpenAI credential in n8n)
1. n8n → **Workflows** → **Import from File** → `n8n/lead-intake-ai-demo.json`. It is **inactive**, uses its own path `lead-intake-ai-demo`, and has no Telegram/WhatsApp nodes. It is not imported by the Docker entrypoint.
2. In **Create Lead in FastAPI** and **Generate AI Insights**, select the existing FastAPI Header Auth credential (the one `lead-intake` uses). Save. Keep it inactive.
3. Open **Prepare Lead Payload** after a test run: `apiBaseUrlSource` must be `environment`. If it reports `workflow-default`, `$env` is blocked and the node would call `http://localhost:8000`; set `DEFAULT_API_BASE_URL` in that node to the API URL.

---

## 6. Safe Manual Test (one synthetic lead)

Prerequisites: code deployed to `leadintel-api`, key and `AI_INSIGHTS_ENABLED=true` set (Section 5), demo workflow imported and bound, still inactive.

1. In the demo workflow click **Execute workflow** (it waits for one test call on the test URL; nothing is activated).
2. From your own terminal send one synthetic lead:
   ```bash
   curl -s -X POST https://leadintel-n8n.onrender.com/webhook-test/lead-intake-ai-demo \
     -H 'Content-Type: application/json' \
     -d '{"name":"Synthetic Prospect","email":"synthetic.prospect@example.com","company":"Example Logistics","source":"ai-demo","external_id":"ai-demo-0001","message":"Please send a quote for 20 sales reps; we are evaluating tools this quarter."}'
   ```
   `source: "ai-demo"` keeps the test lead separate from website leads. Expect `201` with `lead`, the deterministic `score`, and `ai_insights.status: "completed"` (or a `failed` status with one of the error codes above).
3. Verify in the execution view: one `Create Lead in FastAPI` call and one `Generate AI Insights` call.
4. Sending the same payload again returns `409` and makes **no** model call.
5. Optional, from your own shell with the API token already in an environment variable (never typed into chat): `curl -s -H "Authorization: Bearer $LEADINTEL_API_TOKEN" https://leadintel-api-2y0i.onrender.com/leads/<lead_id>/insights`.

Alternative without n8n: with the same token, `POST https://leadintel-api-2y0i.onrender.com/leads/<id>/insights` for an existing synthetic lead.

---

## 7. Known Limitations

- Insights are model output: plausible but possibly wrong. Show them as suggestions; they are not used for routing, scoring or notifications.
- The production `lead-intake` workflow and the website form do not request or display insights; the demo workflow, its JSON response and `GET /leads/{id}/insights` do. Adding them to `lead-intake` or the form is a separate, reviewed change.
- `GET /leads`, `GET /leads/{id}` and the CSV export do not include insights (unchanged contracts).
- `usage` is OpenAI's reported token count per call (stored in `ai_usage`), not a billed amount; the OpenAI dashboard is authoritative for cost.
- Live OpenAI calls are not exercised by automated tests (all mocked). One live call was verified locally (Section 4); the deployed demo path (Section 6) has not been run yet.
- Prompt-injection resistance is best-effort; the impact is bounded to a wrong-but-valid category, urgency or text.
