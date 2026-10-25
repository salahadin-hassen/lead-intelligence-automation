/**
 * Canonical "Attach AI Insights" logic for LeadIntel n8n workflows.
 *
 * Embedded verbatim in the "Attach AI Insights" Code node of
 * n8n/lead-intake.json and n8n/lead-intake-ai-demo.json (the test suite fails
 * if the copies drift apart).
 *
 * Fail-open: lead creation already succeeded, so every problem with the
 * insights call becomes an explicit non-"completed" status. Insights are only
 * attached when the insights response names the lead this execution created;
 * another lead's insights are never returned. Usage, cache flags and any
 * unexpected fields are dropped from the public result.
 */

function attachAiInsights(created, insightsHttp) {
  created = created && typeof created === "object" ? created : {};
  insightsHttp = insightsHttp && typeof insightsHttp === "object" ? insightsHttp : {};
  const createdBody = created.body && typeof created.body === "object" ? created.body : {};
  const leadId = Number.isInteger(createdBody.lead_id) ? createdBody.lead_id : null;

  const FIELDS = ["summary", "intent_category", "urgency", "recommended_next_action", "reason"];
  const CATEGORIES = ["quote_request", "consultation", "support", "partnership", "other"];
  const URGENCIES = ["low", "medium", "high"];
  const NON_COMPLETED = ["disabled", "failed", "pending"];

  const unavailable = (error) => ({ status: "unavailable", lead_id: leadId, insights: null, error });

  let aiInsights;
  const body = insightsHttp.body && typeof insightsHttp.body === "object" ? insightsHttp.body : null;
  if (leadId === null) {
    aiInsights = unavailable("missing_created_lead_id");
  } else if (typeof insightsHttp.statusCode !== "number") {
    // n8n HTTP node error output (timeout, DNS, connection refused, ...)
    aiInsights = unavailable("insights_request_failed");
  } else if (insightsHttp.statusCode !== 200 || !body) {
    aiInsights = unavailable("insights_http_" + insightsHttp.statusCode);
  } else if (body.lead_id !== leadId) {
    aiInsights = unavailable("lead_id_mismatch");
  } else if (body.status === "completed") {
    const raw = body.insights && typeof body.insights === "object" ? body.insights : {};
    const valid =
      FIELDS.every((f) => typeof raw[f] === "string" && raw[f].trim() !== "") &&
      CATEGORIES.includes(raw.intent_category) &&
      URGENCIES.includes(raw.urgency);
    if (valid) {
      const insights = {};
      FIELDS.forEach((f) => { insights[f] = raw[f]; });
      aiInsights = {
        status: "completed",
        lead_id: leadId,
        insights,
        model: typeof body.model === "string" ? body.model : null,
        error: null,
      };
    } else {
      aiInsights = unavailable("invalid_insights");
    }
  } else if (NON_COMPLETED.includes(body.status)) {
    aiInsights = {
      status: body.status,
      lead_id: leadId,
      insights: null,
      error: typeof body.error === "string" ? body.error : null,
    };
  } else {
    aiInsights = unavailable("unexpected_insights_status");
  }

  // Keep the create-lead response shape ({statusCode, body, ...}) intact so
  // the existing downstream nodes read exactly what they read before.
  return Object.assign({}, created, { aiInsights });
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { attachAiInsights };
}
