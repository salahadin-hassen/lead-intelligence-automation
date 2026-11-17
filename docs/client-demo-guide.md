# LeadIntel: Client Live Demo & Presentation Guide

> **A structured 3-to-5 minute live demonstration walkthrough for prospective clients and technical stakeholders, showcasing automated lead ingestion, AI-assisted qualification, and database-level duplicate protection.**

---

## 1. Demo Overview & Preflight Checklist

- **Target Duration:** 3 to 5 minutes.
- **Audience:** Business owners, Heads of Sales/RevOps, Technical Product Managers, or Freelance Clients.
- **Public Demo URL:** `https://leadintel.sahelyon.com`
- **Environment:** Public deployment on Render free tier (separate Caddy, n8n and API services) with serverless Neon PostgreSQL, served through Cloudflare. Last recorded end-to-end production verification: commit `2fc400f`. A read-only check on 2026-11-16 found `/` and `/demo` returning HTTP 200, serving `demo/lead-form.html` from commit `9109f81`. The live n8n workflow and API configuration were not inspected, so the readiness check below doubles as your go/no-go check.

### What This Demo Shows — and What It Does Not

Present only the first group as working functionality.

| Status | Capability |
| :--- | :--- |
| **Demonstrable live (historically live-verified at `2fc400f`)** | Landing page and `/demo` route, form validation, lead intake (HTTP 201), qualification score and reason, duplicate rejection (HTTP 409), request/response details panel. |
| **Visible on the public page, runtime state unverified** | The **"AI lead insights"** box under the score. It is part of the served page, but what it shows depends on the live API configuration and the live n8n workflow, and neither has been verified. See [AI Insights Panel States](#ai-insights-panel-states). |
| **Locally verified only (automated tests, isolated test database)** | Optional LLM scoring with heuristic fallback, Telegram/WhatsApp alert routing, Gmail intake normalization, AI lead insights generation. |
| **Not production-verified — do not present as live services** | LLM scoring (`render.yaml` sets no `LLM_API_KEY`, so scores are expected to come from the deterministic heuristic), WhatsApp delivery, Gmail intake, end-to-end AI insights delivery. |
| **Pending external credentials / manual configuration** | WhatsApp: Meta Developer account, credentials, approved templates. Gmail: Google OAuth credential bound in n8n. AI insights: confirm the live n8n `lead-intake` workflow and the Render settings `AI_INSIGHTS_ENABLED` / `OPENAI_API_KEY` (rollout steps in `docs/ai-lead-insights.md` §8). |

Telegram alert delivery was verified in a local rehearsal (execution 135) and is not visible to the client on screen; describe it, do not claim to show it.

### Pre-Call Readiness Check (Cold-Start Mitigation)

Render's free tier puts each service to sleep after 15 minutes without traffic, and each one wakes **independently**. Caddy serves the landing page itself, so a fast-loading page does **not** mean the API or n8n are awake. Any keep-warm ping aimed at the site does not wake them either.

What a cold start looked like on 2026-11-16:

| Service | Cold-start observation |
| :--- | :--- |
| API `GET /health` | ~23.3 s |
| n8n `GET /healthz` | First request timed out after 150 s. A retry answered after ~96.9 s, about 4 minutes after the first request. |
| Demo form | Aborts each request after **120 s** |

A cold n8n can take longer than the form's 120-second timeout, so wake each service on its own using its health URL. Start **at least 15 minutes before the meeting**:

1. **Wake the API:** open `https://leadintel-api-2y0i.onrender.com/health` in a browser tab and wait for `{"status":"ok"}`. If the tab times out or errors, reload it.
2. **Wake n8n:** open `https://leadintel-n8n.onrender.com/healthz` in another tab and wait for `{"status":"ok"}`. Expect this to take several minutes, and the first attempt may time out; reload until it answers.
3. **Load the page:** open `https://leadintel.sahelyon.com/` in an Incognito/Private tab, click **"Try demo →"**, and check that the form renders. **Do not submit anything.**
4. **Keep them awake:** each service sleeps again after 15 minutes without traffic. If more than ~10 minutes will pass before the live submission, reload both health URLs shortly before you start.

**What the health checks prove, and what they don't.** The two health URLs are read-only and create no data. `{"status":"ok"}` only means the service process is up and answering; the API check also confirms it can reach its database. Neither check runs the intake workflow. They don't prove that a submission will be accepted, scored, deduplicated or alerted, or which state the AI-insights box will show. The first live submission of the meeting is therefore the first end-to-end run, so be ready for any of the [AI Insights Panel States](#ai-insights-panel-states).

**No warm-up submissions on production.** Do not submit a lead on `leadintel.sahelyon.com` just to warm services up or to preview results. Every production submission creates a real lead record and can trigger operator notifications (Telegram, and WhatsApp once configured) and, depending on configuration, a paid AI call. A rehearsal submission is acceptable only:
- in an isolated synthetic/demo environment that is not connected to the production database, notification channels or AI keys, such as the local stack in [`deployment.md`](deployment.md); **or**
- under an explicitly approved production test procedure that defines the synthetic test data, how notifications and AI calls are suppressed or expected, and how the test records are identified afterwards.

No such production test procedure is documented yet.

**Go/no-go decision:**

| Check | GO | NO-GO |
| :--- | :--- | :--- |
| API `/health` | `{"status":"ok"}` | No answer after several reloads, or any error |
| n8n `/healthz` | `{"status":"ok"}` | No answer after several reloads (allow ~5 minutes), or any error |
| Landing page and `/demo` | Page and form render | Page fails to load |
| Time since last health check | Under ~10 minutes, or re-checked | Over 15 minutes and not re-checked |

**GO** only when every row is GO. You then run the live walkthrough, knowing the first submission is the first end-to-end run. On **NO-GO**, don't run the live walkthrough; use a recorded walkthrough or the repository tour in [`project-evaluation-guide.md`](project-evaluation-guide.md) instead. If the first live submission fails during the meeting, follow the [Contingency Playbook](#3-contingency-playbook-handling-free-tier-cold-starts).

---

## 2. Step-by-Step Live Walkthrough Script (3–5 Minutes)

```
0:00 ──────────────── 0:45 ──────────────── 2:00 ──────────────── 3:15 ──────────────── 4:30
  Hook & Navigation         High-Intent Intake        Duplicate Rejection       Decoupled Architecture
```

---

### Step 1: The First Impression & Frictionless Navigation (0:00 – 0:45)

**Goal:** Establish the business problem and show a polished, professional user interface.

- **Action:** Share your screen with `https://leadintel.sahelyon.com/` open.
- **What to Observe:** The landing page opens cleanly at the top of the viewport (`scrollY: 0`). The interactive demo form is cleanly separated from the marketing copy.
- **Action:** Click the header **"Try demo →"** button or the **"Interactive demo"** nav link.
- **Expected Result:** The browser transitions smoothly to the dedicated route `https://leadintel.sahelyon.com/demo` at `scrollY: 0`, focusing user attention directly on the interactive lead intake card.
- **Presenter Script:**
  > *"When a prospective client lands on your website, speed and clarity matter. But most inbound lead forms are either disconnected from backend automation or dump data into a shared spreadsheet.*
  >
  > *LeadIntel is built to capture, qualify, and route B2B leads automatically. Notice that we keep our marketing presentation and interactive demo cleanly decoupled. Let’s look at how the intake pipeline behaves when a high-intent prospect submits an inquiry."*

---

### Step 2: Inbound Ingestion & Real-Time Intent Scoring (0:45 – 2:00)

**Goal:** Demonstrate clean form validation, immediate user feedback, and transparent intent scoring.

- **Action:** On the `/demo` page, click the preset **"Enterprise buyer"** (or enter realistic synthetic data, for example):
  - **Full Name:** `Alex Carter`
  - **Work Email:** `alex@enterprise-acme.com` *(notice corporate domain)*
  - **Company:** `Acme Corporation`
  - **Inquiry Details:** `Evaluating enterprise automation for our 200-person sales team. Need pricing, security review, and integration demo.`
- **Action:** Click **"Send demo enquiry →"**.
- **Expected Result:**
  1. The submit button immediately enters a disabled state with an active loading indicator ("Sending to intake…").
  2. Within typical warm response times (observed around 4–5 seconds during verification), the form transitions to an emerald confirmation panel: **"Enquiry accepted"**.
  3. The **Qualification score** gauge displays a high score with the detected rationale. With the deterministic heuristic scorer, the "Enterprise buyer" preset scores **100/100** and the Alex Carter example **95/100** (reason e.g. *"High-intent keywords (demo, pricing, enterprise, integration); substantial message; corporate email domain"*).
  4. Below the score, the public page shows an **"AI lead insights"** box. Its content depends on the live backend and is **not guaranteed**. Describe whichever state appears (see [AI Insights Panel States](#ai-insights-panel-states)), and never promise that generated insights will appear.
- **Presenter Script:**
  > *"When Alex clicks submit, three critical things happen behind the scenes:*
  >
  > *First, the client generates a unique submission key (`external_id`) to identify this specific inquiry.*
  > *Second, the inquiry is validated and safely committed to a relational PostgreSQL database.*
  > *Third, a deterministic scoring engine analyzes the prospect’s purchase intent (an LLM scorer can be switched on, with automatic fallback). Because Alex used a corporate domain and requested enterprise pricing and integration for a 200-person team, the system scores this lead at the highest priority level.*
  >
  > *Notice that Alex receives an immediate, professional confirmation without having to wait for downstream operator notifications to process."*

#### AI Insights Panel States

The box's badge depends on what the live n8n workflow and API return for this particular submission. None of these states has been confirmed as current production behaviour, and the health checks can't reveal which one will appear, so be ready for any of them.

| Badge | What it means | What to say |
| :--- | :--- | :--- |
| **Not returned** | The intake response contained no `ai_insights`. This is what happens if the live n8n workflow doesn't request insights. | *"The lead was saved and scored; AI suggestions are an optional add-on that isn't enabled on this demo."* |
| **Disabled** | The API reports AI insights turned off (`AI_INSIGHTS_ENABLED` not enabled). | Same as above. |
| **Failed** | An AI call was attempted but didn't produce a result (timeout, rate limit, exhausted quota or budget cap, or invalid output). | *"The lead was saved and scored regardless; AI suggestions never block intake."* |
| **Generated** (successful insights) | Summary, intent, urgency, recommended next action and reasoning, labelled with the model name. | Present them as AI-generated suggestions for a sales rep to review, not decisions. The score is calculated separately and is unchanged by AI. |

The page can also show **Unavailable** (for example a response for a different lead, or an HTTP error from the insights call) or **Pending**. Treat them like **Failed**. In every state the lead is still saved and scored.

---

### Step 3: Demonstrating Database-Enforced Duplicate Protection (2:00 – 3:15)

**Goal:** Demonstrate data integrity that protects sales reps from redundant records and split attribution.

- **Action:** In the green outcome panel, point out the action button: **"Re-submit (Test Duplicate)"**.
- **Action:** Click the button to resubmit the identical lead inquiry.
- **Expected Result:**
  1. The intake pipeline processes the request within typical warm response times.
  2. The UI renders an amber warning panel: **"Duplicate Ingestion Blocked (409)"**.
  3. The message confirms: *"The API rejected this source and external ID pair; no duplicate lead was created."*
- **Presenter Script:**
  > *"In real business operations, prospects double-click submit buttons, refresh their confirmation tabs, or submit the same form twice across different marketing campaigns.*
  >
  > *With basic webhook setups, that creates duplicate records, assigns multiple reps to the same account, and pollutes reporting. With LeadIntel, duplicate prevention is enforced directly at the database engine level via a unique constraint.*
  >
  > *When we replayed that exact inquiry with the same submission key, the database rejected the insert and returned an HTTP 409 Conflict. The system acknowledged the inquiry cleanly, but refused to create a duplicate row. Your sales pipeline stays clean."*

---

### Step 4: Behind the Scenes — Decoupled Routing & Reliability (3:15 – 4:15)

**Goal:** Explain the technical architecture, business guarantees, and failure isolation without overwhelming the client.

- **Action:** Click **"View request / response details"** on the demo page to show the request and response JSON (or reference the architecture overview).
- **Presenter Script:**
  > *"Here is why this architecture is more robust than standard no-code automations:*
  >
  > *1. **Decoupled Responsibilities:** FastAPI handles data integrity, validation, and calculations. n8n handles routing and external messaging. PostgreSQL enforces database rules.*
  >
  > *2. **Automatic AI Fallback:** When optional LLM scoring is switched on and the external model times out or hits an API rate limit, the system does not crash or drop the lead. It automatically switches to an offline, deterministic heuristic analyzer based on keyword signals and domain authority — the same analyzer that scored the lead you just saw.*
  >
  > *3. **Caller-Response Isolation:** Our webhook confirms the lead to the visitor as soon as it is safely stored in PostgreSQL. If downstream notification channels experience a temporary outage, the lead is already safe in the database and the customer never sees a broken 500 error.*
  >
  > *4. **Smart Notification Routing:** Leads scoring 70 and above trigger a Telegram alert to your sales reps. Lower-intent inquiries are logged quietly in the database without distracting your team. WhatsApp alerts are already wired into the same routing and go live once your Meta Business credentials are connected."*

---

### Step 5: Wrap-Up & Customization Potential (4:15 – 5:00)

**Goal:** Pivot from the demo to the client's specific business needs.

- **Action:** Click **"← Back to overview"** to return smoothly to the marketing landing page.
- **Presenter Script:**
  > *"What we’ve just seen is a working reference architecture for website intake, scoring and deduplication. It can be tailored to your specific workflow:*
  >
  > *- We can customize the scoring algorithm to match your exact Ideal Customer Profile (ICP).*
  > *- We can route alerts to your team’s Telegram today, and to WhatsApp once Meta credentials are set up; Slack or Microsoft Teams would be new integration work.*
  > *- Downstream tools like HubSpot, Salesforce, or your custom database would be new integration work; CRM sync is not built yet.*
  >
  > *What questions do you have about adapting this pipeline for your team?"*

---

## 3. Contingency Playbook: Handling Free-Tier Cold Starts

Because the public demo sandbox is deployed on Render's zero-cost tier, you can still hit a cold start if you skipped the [pre-call readiness check](#pre-call-readiness-check-cold-start-mitigation) or the meeting started more than 15 minutes after it.

### If a Submission Is Slow or Times Out

1. **Stay Calm & Point Out the Loading State:** The UI shows a loading spinner and disables the button, preventing double-clicks.
2. **Use the Opportunity as an Architectural Talking Point:**
   > *"Notice that the interface is awaiting confirmation. Our public demo sandbox is hosted on a zero-cost cloud tier where services sleep when idle and wake on demand. In a commercial deployment with dedicated instances, services stay warm and this delay doesn't occur."*
3. **If the green outcome card arrives:** continue the walkthrough as normal.
4. **If the request times out:** after 120 s the page shows *"No response was received from the intake service. It may still be waking up…"*. A cold n8n can take several minutes, so the result may arrive only after the browser has given up. Open `https://leadintel-n8n.onrender.com/healthz` in another tab and wait for `{"status":"ok"}`. Then click submit again **without editing any field or clicking a preset**, since either would generate a new submission key:
   - **"Enquiry accepted"** means the first request never reached the database.
   - **"Duplicate Ingestion Blocked (409)"** means the first request was saved after all. You can present this as the duplicate-protection step.
5. **If n8n still doesn't answer:** stop the live demo and switch to the recorded walkthrough.

---

## 4. Privacy & Demonstration Boundaries

To maintain professional security standards during live client calls and recordings:

- **Use Only Synthetic Data:** Always use realistic synthetic company names (`Acme Corp`, `NovaLogistics`, `Vertex Solutions`) and demo email addresses (`alex@example.com`).
- **Never Reveal Infrastructure Secrets:** Do not open `.env` files, database connection strings, n8n internal credentials, or Telegram API bot tokens during screen shares.
- **Stick to the Public Web Experience:** Conduct the walkthrough entirely through the browser at `https://leadintel.sahelyon.com`. Showing the polished, working web portal builds vastly more client confidence than clicking through terminal logs or backend database tables.
