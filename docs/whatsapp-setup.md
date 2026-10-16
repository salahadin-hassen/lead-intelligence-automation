# WhatsApp Business Cloud API Integration Guide

## Overview

The Lead Intelligence Automation pipeline integrates WhatsApp Business Cloud API as an internal operator alerting channel. When an inbound lead is processed:
- **Qualified leads** (score $\ge 70$) trigger a priority notification with lead metadata to the configured sales/operations recipient.
- **Manual review leads** (score is null or unscoreable) trigger an alert highlighting the need for operator review.
- **Not-qualified leads** (score $< 70$) **never** trigger WhatsApp notifications.

> [!NOTE]
> WhatsApp alerts are strictly internal notifications dispatched to the business owner or sales team. The customer/lead who submitted the contact form is never contacted via WhatsApp, and free-text customer inquiries are omitted to prevent sensitive data leaks.

---

## 1. Prerequisites & Meta Developer Setup

To deliver live WhatsApp notifications via Meta's official Cloud API, an operator must configure a Meta Developer application:

1. **Meta Developer Account**:
   - Register at [developers.facebook.com](https://developers.facebook.com/).
   - Create an app of type **Business**.
2. **Add WhatsApp Product**:
   - In the App Dashboard, select **Add Product** and choose **WhatsApp**.
   - Navigate to **WhatsApp > API Setup**.
3. **Sender Phone Number**:
   - Meta provides a free **Test Phone Number** for sandbox validation.
   - For production, register and verify a dedicated business phone number in Meta Business Manager.
4. **Access Tokens**:
   - **Sandbox / Development**: Meta provides a 24-hour temporary access token on the API Setup page.
   - **Production**: Create a **System User** under Meta Business Manager:
     - Assign the WhatsApp Business Account asset.
     - Generate a permanent access token with permissions: `whatsapp_business_messaging` and `whatsapp_business_management`.
5. **Recipient Phone Numbers**:
   - For test accounts, recipient numbers must be explicitly added to the **To** recipient allowlist in the WhatsApp API Setup dashboard and verified via OTP.
   - In production, messages can be delivered to any valid WhatsApp user subject to Meta messaging policies.

---

## 2. Configuration & Environment Variables

### Process Environment (`deploy/env/n8n.env`)

Add the following variables to `deploy/env/n8n.env` (copy from `deploy/env/n8n.env.example`):

```bash
# Workflow Code nodes read process environment
N8N_BLOCK_ENV_ACCESS_IN_NODE=false

# Meta Phone Number ID (from Meta Developer Console -> WhatsApp -> API Setup)
WHATSAPP_PHONE_NUMBER_ID=109283746501928

# Destination operator phone number in E.164 format WITHOUT '+' (e.g., 15551234567)
WHATSAPP_RECIPIENT_PHONE_NUMBER=15551234567
```

> [!CAUTION]
> Never commit real tokens or phone numbers to version control. Keep `deploy/env/n8n.env` in `.gitignore`.

### n8n Credential Store

In the n8n web interface:
1. Navigate to **Settings > Credentials > New Credential**.
2. Select **WhatsApp Business Cloud API** (`whatsAppApi`).
3. Set **Credential Name**: `WhatsApp Lead Alerts`.
4. Enter the permanent Access Token obtained from Meta Business Manager.
5. Save the credential. The workflow node references this credential by ID (`whatsapp-lead-alerts`).

---

## 3. Messaging Rules & Approved Template Schema

Meta enforces strict conversational policies:
- **Freeform Text Messages**: Allowed only within an active **24-hour customer service window** (initiated when the recipient sends an inbound message to the business number).
- **Business-Initiated Notifications**: Outbound operator alerts initiated by the system outside an open 24-hour window **must use approved Message Templates**.

### Template 1: Qualified Lead Alert

- **Template Name**: `lead_qualified_alert`
- **Category**: `UTILITY`
- **Language**: `en_US`
- **Header**: None
- **Body Content**:
  ```text
  🔥 QUALIFIED LEAD ALERT

  Lead ID: {{1}}
  Name: {{2}}
  Company: {{3}}
  Email: {{4}}
  Score: {{5}}
  Source: {{6}}

  Action: Review lead details and initiate priority follow-up.
  ```
- **Parameter Mapping**:
  1. `{{1}}` $\to$ `leadId` (e.g., `42`)
  2. `{{2}}` $\to$ `name` (e.g., `Ada Lovelace`)
  3. `{{3}}` $\to$ `company` (e.g., `Analytical Engines Ltd`)
  4. `{{4}}` $\to$ `email` (e.g., `ada@company.com`)
  5. `{{5}}` $\to$ `score` (e.g., `85`)
  6. `{{6}}` $\to$ `source` (e.g., `website-contact-form`)

### Template 2: Manual Review Alert

- **Template Name**: `lead_manual_review_alert`
- **Category**: `UTILITY`
- **Language**: `en_US`
- **Body Content**:
  ```text
  ⚠️ LEAD NEEDS MANUAL REVIEW

  Lead ID: {{1}}
  Name: {{2}}
  Company: {{3}}
  Email: {{4}}
  Score: {{5}}
  Source: {{6}}
  Reason: {{7}}

  Action: Inspect lead record in intake queue to determine routing.
  ```
- **Parameter Mapping**:
  1. `{{1}}` $\to$ `leadId` (e.g., `43`)
  2. `{{2}}` $\to$ `name` (e.g., `Charles Babbage`)
  3. `{{3}}` $\to$ `company` (e.g., `Difference Engine Corp`)
  4. `{{4}}` $\to$ `email` (e.g., `charles@engine.org`)
  5. `{{5}}` $\to$ `score` (e.g., `unavailable`)
  6. `{{6}}` $\to$ `source` (e.g., `website-contact-form`)
  7. `{{7}}` $\to$ `reason` (e.g., `scoring unavailable`)

---

## 4. Failure Isolation & Operational Safeguards

The workflow isolates notification delivery from the primary lead ingestion transaction:

```text
n8n Webhook
    ↓
FastAPI Lead Creation (201 Created)
    ↓
Route on API Status
    ├── 1. Respond Created (immediate HTTP 201 to browser)
    └── 2. Qualification Engine
              ↓
         Log Priority Route
              ├── A. Build Audit Event (persisted in n8n DB)
              ├── B. Telegram Notification (independent branch)
              └── C. WhatsApp Notification (onError: continueErrorOutput)
                       ├── Success -> Log WhatsApp Qualified Success
                       └── Failure -> Log WhatsApp Qualified Failure
```

1. **HTTP 201 Isolation**: `Respond Created` runs before external notification dispatch. If Meta's API times out or rejects the message, the client has already received their 201 Created acknowledgment.
2. **Database Integrity**: The lead is committed in PostgreSQL by FastAPI prior to notification dispatch. WhatsApp errors can never roll back or corrupt the database.
3. **Decoupled Telegram Alerts**: Telegram and WhatsApp operate as independent sibling branches. A failure in WhatsApp delivery does not abort or prevent Telegram alerts.
4. **Error Handling (`continueErrorOutput`)**: Both WhatsApp send nodes configure `"onError": "continueErrorOutput"`. When an error occurs (such as invalid tokens, unverified test numbers, or template mismatches), execution routes to an operational failure logging node without failing the entire workflow.

---

## 5. How to Disable WhatsApp Alerts

If WhatsApp notifications are not needed or during local development:
- **Option 1 (Process config)**: Do not set `WHATSAPP_PHONE_NUMBER_ID` in `deploy/env/n8n.env`. The workflow detects the missing configuration and safely logs the skipped state.
- **Option 2 (n8n canvas)**: In n8n, click on `Send Qualified to WhatsApp` and `Send Manual Review to WhatsApp` and toggle the **Disable Node** switch. The rest of the workflow (persistence, scoring, audit, Telegram) operates normally.

---

## 6. Verification Status

| Verification Scope | Status | Notes |
| :--- | :--- | :--- |
| **Workflow Schema & Routing** | Verified | Validated node graph, connections, parameters, and error handlers. |
| **Data Contract & Sanitation** | Verified | Passes lead ID, name, company, email, source; strips free-text body. |
| **Failure Isolation** | Verified | `continueErrorOutput` ensures zero interference with HTTP response or Telegram. |
| **Live Meta API Delivery** | Unverified / Pending Credentials | Requires active Meta Developer Account and verified recipient phone number. |
