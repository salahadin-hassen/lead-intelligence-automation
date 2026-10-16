# 3-Minute Demo Walkthrough Script: Lead Intelligence Automation

**Target Duration:** 2.5 to 3 minutes
**Format:** Screen recording with voiceover (split view or sequential browser tabs)
**Tone:** Professional, direct, technical, and evidence-driven

---

## Preparation & Setup Before Recording

1. **Browser Tab 1:** The contact form (local or rehearsal URL, e.g., `http://localhost:8080/` or `demo/lead-form.html`).
2. **Browser Tab 2:** The n8n execution view showing the `Lead Intake & Qualification` workflow.
3. **Browser Tab 3 / Terminal:** Read-only execution inspector (`python3 deploy/bin/n8n_exec.py list`) or a clean PostgreSQL client query window.
4. **Data Privacy Guard:** Use **only synthetic demo data** (e.g., `alex.carter@enterprise-acme.com`). Never display real personal emails, API keys, Telegram bot tokens, or database passwords on screen.

---

## Walkthrough Timeline

### 0:00 – 0:20 | The Problem
**On Screen:** Show the clean contact form in Tab 1.

**Spoken Narration:**
> "Inbound B2B lead generation often breaks down at the handoff. When a prospective customer submits a contact inquiry, companies struggle with slow triage, duplicate inquiries, and fragile automations that fail silently when third-party services go down.
>
> In this three-minute walkthrough, I’ll demonstrate **Lead Intelligence Automation**—an intake and qualification system engineered with FastAPI, PostgreSQL, and n8n that captures inbound leads, prevents duplicates, evaluates buying intent with a hybrid AI/heuristic scorer, and delivers immediate sales alerts with strict failure isolation."

---

### 0:20 – 0:55 | Step 1: Submit a Synthetic High-Intent Lead
**On Screen:** Fill out the contact form with synthetic B2B details:
- **Name:** `Alex Carter`
- **Email:** `alex@acme-corp.com` (corporate domain)
- **Company:** `Acme Corporation`
- **Message:** `We are evaluating enterprise solutions and need pricing and an integration demo for our 200-person team.`
- Click **Submit**. The submit button disables, showing "Submitting…", and immediately transitions to the green **"Thanks. Your message has been received"** success panel.

**Spoken Narration:**
> "Let’s submit a realistic high-intent inbound inquiry. The form submits directly through our ingress proxy to our n8n webhook.
>
> Notice the instant confirmation. The client-side form generates a unique per-submission `external_id`, which guarantees idempotency if the user accidentally double-clicks or retries after a network blip."

---

### 0:55 – 1:25 | Step 2: Orchestration & Scoring in n8n
**On Screen:** Switch to Tab 2 (n8n execution list and click the latest execution graph). Show the green execution path: `Lead Webhook` $\rightarrow$ `Create Lead in FastAPI` $\rightarrow$ `Respond Created` $\rightarrow$ `Qualification` $\rightarrow$ `Log Priority Route`.

**Spoken Narration:**
> "Here is the live execution inside n8n. Notice the architecture boundary: n8n doesn’t execute SQL or scoring algorithms directly. It passes the payload to our FastAPI backend.
>
> FastAPI validated the input, stored it, and ran it through our scoring engine. Because the message contained high-intent keywords like 'pricing', 'enterprise', and 'demo' from a corporate domain, it received a qualification score of 100.
>
> In n8n, our qualification node compares that score against our threshold of 70 and routes it to the `Log Priority Route`."

---

### 1:25 – 1:55 | Step 3: Database Persistence & Deduplication
**On Screen:** Switch to Terminal / DB query view. Show the single inserted row in the `leads` table with columns: `id`, `name`, `email`, `company`, `score: 100`, `status: new`.

**Spoken Narration:**
> "Looking at the PostgreSQL database, the lead was persisted cleanly with a raw, parameterized SQL query. All data integrity rules—including score boundaries from 0 to 100 and valid status values—are enforced directly by database `CHECK` constraints, not just application code.
>
> Furthermore, our compound unique constraint on `(source, external_id)` guarantees that resubmitting the same request returns an HTTP 409 Conflict at the database level rather than creating duplicate records."

---

### 1:55 – 2:25 | Step 4: Operational Audit, Telegram & WhatsApp Notifications
**On Screen:** In n8n, click the `Build Audit Event` node output, `Send Qualified to Telegram`, and `Send Qualified to WhatsApp` nodes. Show the structured JSON containing `event: lead_qualified`, `priority: high`, and Telegram response showing `ok: true`, `message_id: 25`.

**Spoken Narration:**
> "For operational visibility, every processed lead generates an execution-native audit event with full provenance and a human-readable summary.
>
> For actionable outcomes—qualified leads and unscored leads requiring manual review—the system dispatches formatted alerts to both our sales Telegram chat and WhatsApp Business Cloud API. In verified test runs (such as execution 135), the Telegram Bot API confirmed delivery with a message ID.
>
> Low-intent leads scoring below 70 route to normal logging without interrupting the sales team."

---

### 2:25 – 2:50 | Step 5: Defensive Reliability & Failure Isolation
**On Screen:** Briefly highlight the `Respond Created` node position relative to the notification chain in the canvas.

**Spoken Narration:**
> "What makes this system production-ready is how it handles failure.
>
> First, our AI scorer has an automatic fallback: if OpenRouter or OpenAI times out or fails, the system switches seamlessly to a local, deterministic heuristic scorer—intake never breaks.
>
> Second, we engineered caller-response isolation: `Respond Created` executes *before* downstream audit, Telegram, or WhatsApp alerts. If external messaging providers experience an outage, the lead is already safe in PostgreSQL and the prospect receives their 201 confirmation. The failure is isolated and visible to operators in n8n logs."

---

### 2:50 – 3:00 | Close
**On Screen:** Return to the GitHub repository / `README.md` view.

**Spoken Narration:**
> "Lead Intelligence Automation combines the speed of modern workflow automation with the rigor of enterprise software engineering: 97 automated tests, database-enforced integrity, and verified resilience.
>
> All architecture specifications, failure drills, and case studies are fully documented in the repository. Thank you for watching."

---

## Recording Checklist

- [ ] Video resolution set to 1080p (1920x1080).
- [ ] Font size in code/terminal/browser zoomed to 125%–150% for mobile readability.
- [ ] No real credentials, private chat IDs, or actual email addresses visible.
- [ ] Pace is steady, matching the target 2.5–3.0 minute mark.

---

## Recommended Portfolio Screenshots (Static Images)

If capturing static screenshots for case studies or LinkedIn/portfolio slides instead of video, capture exactly these three views:

1. **The Inbound Contact Form (`demo/lead-form.html`):**
   - Clean, professional view of the form filled with synthetic B2B inquiry data, highlighting accessible error states and clean styling.
2. **The n8n Execution Graph View:**
   - The full visual pipeline showing a successful green execution run (`execution 135` or equivalent): `Lead Webhook` $\rightarrow$ `FastAPI` $\rightarrow$ `Respond Created` $\rightarrow$ `Qualification` $\rightarrow$ `Log Priority Route` $\rightarrow$ `Send Qualified to Telegram`.
3. **The Telegram Notification & Persistence Proof:**
   - Split view showing the received sales alert on Telegram (`High Priority Lead`, company name, score, reason) alongside the verified database row in PostgreSQL or the structured audit JSON in n8n.
   - *Privacy Check:* Ensure Telegram bot token, chat ID numbers, and private machine paths are fully cropped or redacted.

