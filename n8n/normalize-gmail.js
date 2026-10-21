/**
 * Canonical email normalization logic for LeadIntel Gmail lead ingestion.
 *
 * Used inside n8n/gmail-lead-intake.json ("Normalize Email Lead" Code node)
 * and verified by automated test suites.
 */

function normalizeGmailLead(item, config) {
  item = item || {};
  const headers = item.headers || {};

  // 1. Resolve message ID (external_id)
  const messageId = String(item.id || headers["message-id"] || "").trim();
  if (!messageId) {
    return {
      status: "skip",
      reason: "malformed_data",
      detail: "Missing stable Gmail message ID",
      item: { id: null }
    };
  }

  // 2. Resolve From header and extract email & display name
  let rawFrom = "";
  if (typeof item.from === "string") {
    rawFrom = item.from;
  } else if (item.from && typeof item.from.text === "string") {
    rawFrom = item.from.text;
  } else if (item.from && Array.isArray(item.from.value) && item.from.value[0]) {
    const val = item.from.value[0];
    rawFrom = val.name ? `${val.name} <${val.address}>` : (val.address || "");
  } else if (typeof headers.from === "string") {
    rawFrom = headers.from;
  }

  rawFrom = rawFrom.trim();

  let senderName = "";
  let senderEmail = "";

  const angleMatch = rawFrom.match(/^(.*?)\s*<([^>]+)>$/);
  if (angleMatch) {
    senderName = angleMatch[1].replace(/^["'\s]+|["'\s]+$/g, "").trim();
    senderEmail = angleMatch[2].trim();
  } else if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(rawFrom)) {
    senderEmail = rawFrom;
  } else {
    const match = rawFrom.match(/([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})/);
    if (match) {
      senderEmail = match[1];
      senderName = rawFrom.replace(match[0], "").replace(/[<>()"']/g, "").trim();
    }
  }

  // Basic email validation
  if (!senderEmail || !/^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/.test(senderEmail)) {
    return {
      status: "skip",
      reason: "malformed_data",
      detail: "Missing or unparseable sender email in From header",
      messageId
    };
  }

  // 3. Resolve Subject
  const subject = String(item.subject || headers.subject || "").trim();

  // 4. Automated notice / bounce / auto-reply detection
  const lowerSubject = subject.toLowerCase();
  const lowerFrom = rawFrom.toLowerCase();
  const autoSubmitted = String(headers["auto-submitted"] || "").toLowerCase();

  const isAutomated = (
    autoSubmitted === "auto-replied" ||
    autoSubmitted === "auto-generated" ||
    lowerFrom.includes("mailer-daemon@") ||
    lowerFrom.includes("postmaster@") ||
    lowerFrom.includes("noreply@") ||
    lowerFrom.includes("no-reply@") ||
    lowerSubject.startsWith("automatic reply:") ||
    lowerSubject.startsWith("out of office:") ||
    lowerSubject.includes("delivery status notification") ||
    lowerSubject.includes("undelivered mail") ||
    lowerSubject.includes("failure notice")
  );

  if (isAutomated) {
    return {
      status: "skip",
      reason: "automated_notice",
      detail: "Detected automated notice, delivery failure, or out-of-office message",
      messageId,
      senderEmail,
      subject
    };
  }

  // 5. Body Extraction and Cleaning
  let rawBody = "";
  if (typeof item.text === "string" && item.text.trim()) {
    rawBody = item.text;
  } else if (typeof item.html === "string" && item.html.trim()) {
    // Convert HTML to clean plain text
    rawBody = item.html
      .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, "")
      .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, "")
      .replace(/<br\s*[\/]?>/gi, "\n")
      .replace(/<\/(p|div|tr|li|h[1-6])>/gi, "\n")
      .replace(/<[^>]+>/g, "")
      .replace(/&nbsp;/gi, " ")
      .replace(/&amp;/gi, "&")
      .replace(/&lt;/gi, "<")
      .replace(/&gt;/gi, ">")
      .replace(/&quot;/gi, "\"")
      .replace(/&#39;/gi, "'");
  } else if (typeof item.snippet === "string") {
    rawBody = item.snippet;
  }

  // Strip reply chains & email quotes
  const bodyLines = rawBody.split(/\r?\n/);
  const cleanedLines = [];
  for (const line of bodyLines) {
    const trimmed = line.replace(/[ \t]+/g, " ").trim();
    // Stop or strip quoted lines
    if (/^>/.test(trimmed)) continue;
    if (/^on\s+.*wrote:$/i.test(trimmed)) break;
    if (/^-----Original Message-----/i.test(trimmed)) break;
    if (/^From:\s.*Sent:\s/i.test(trimmed)) break;
    cleanedLines.push(trimmed);
  }

  let cleanedBody = cleanedLines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
  // Safe sensible length cap (3000 chars)
  if (cleanedBody.length > 3000) {
    cleanedBody = cleanedBody.slice(0, 3000) + "… [truncated]";
  }

  const finalMessage = subject
    ? (cleanedBody ? `Subject: ${subject}\n\n${cleanedBody}` : `Subject: ${subject}`)
    : (cleanedBody || "[Inbound email inquiry with no text body]");

  // 6. Name derivation
  if (!senderName) {
    const localPart = senderEmail.split("@")[0].replace(/[._+-]+/g, " ").trim();
    if (localPart && !/^\d+$/.test(localPart)) {
      senderName = localPart
        .split(" ")
        .map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
        .join(" ");
    } else {
      senderName = "Direct Inquirer";
    }
  }

  // 7. Company derivation
  const freeMailDomains = new Set([
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "proton.me",
    "protonmail.com", "aol.com", "mail.com", "gmx.com", "zoho.com", "yandex.com"
  ]);

  const domain = senderEmail.split("@")[1].toLowerCase();
  let company = "";

  if (!freeMailDomains.has(domain)) {
    // Derive corporate company name from domain (e.g. "acme-corp.co.uk" -> "Acme Corp")
    const mainPart = domain.split(".")[0];
    company = mainPart
      .replace(/[-_]+/g, " ")
      .split(" ")
      .map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
      .join(" ");
  } else {
    // Check if message explicitly mentions "Company: ..."
    const compMatch = cleanedBody.match(/\b(?:company|organization|firm|agency)\s*[:=-]\s*([^\r\n,]+)/i);
    if (compMatch && compMatch[1].trim()) {
      company = compMatch[1].trim();
    } else {
      company = "Unspecified";
    }
  }

  const apiBase = (config && config.apiBaseUrl) || process.env.LEAD_API_BASE_URL || "https://leadintel-api-2y0i.onrender.com";
  const apiLeadsUrl = `${apiBase.replace(/\/+$/, "")}/leads`;

  return {
    status: "valid",
    lead: {
      name: senderName,
      email: senderEmail,
      company: company,
      message: finalMessage,
      source: "gmail",
      external_id: messageId
    },
    apiLeadsUrl: apiLeadsUrl,
    qualifiedThreshold: (config && config.qualifiedThreshold) || 70,
    thresholdSource: (config && config.thresholdSource) || "default_policy"
  };
}

// Support CommonJS and ES Module exports
if (typeof module !== "undefined" && module.exports) {
  module.exports = { normalizeGmailLead };
}
