/**
 * Cloudflare Pages Function & Worker: Webhook Intake Proxy
 *
 * Route: POST /webhook/lead-intake
 *
 * Narrowly scoped proxy that forwards inbound lead form submissions to the
 * verified Render ingress endpoint (https://leadintel-caddy.onrender.com/webhook/lead-intake).
 *
 * Compatible with:
 * - Cloudflare Pages Functions (invoked via `export async function onRequest`)
 * - Cloudflare Workers with Assets (invoked via `export default { fetch }` from wrangler.json)
 *
 * Guarantees:
 * - Restricts requests strictly to this exact upstream endpoint.
 * - Accepts only HTTP POST; rejects unsupported methods with HTTP 405.
 * - Preserves upstream HTTP status codes (e.g., 201 Created, 409 Conflict, 422 Unprocessable).
 * - Returns a sanitized HTTP 502 response on upstream network failure.
 * - Does not log lead details, user PII, or internal tokens.
 */

const UPSTREAM_WEBHOOK_URL = "https://leadintel-caddy.onrender.com/webhook/lead-intake";

async function handleWebhookProxy(request) {
  // 1. Restrict methods: Only POST is accepted
  if (request.method !== "POST") {
    return new Response(
      JSON.stringify({
        outcome: "method_not_allowed",
        apiStatus: 405,
        detail: "Method Not Allowed. Only POST requests are accepted.",
      }),
      {
        status: 405,
        headers: {
          "Content-Type": "application/json",
          "Allow": "POST",
        },
      }
    );
  }

  // 2. Read request body safely
  let rawBody;
  try {
    rawBody = await request.text();
  } catch (err) {
    return new Response(
      JSON.stringify({
        outcome: "bad_request",
        apiStatus: 400,
        detail: "Unable to read request body.",
      }),
      {
        status: 400,
        headers: {
          "Content-Type": "application/json",
        },
      }
    );
  }

  // 3. Proxy request to exact upstream URL
  try {
    const upstreamHeaders = new Headers();
    const contentType = request.headers.get("content-type");
    upstreamHeaders.set("Content-Type", contentType || "application/json");

    const accept = request.headers.get("accept");
    if (accept) {
      upstreamHeaders.set("Accept", accept);
    }

    const upstreamResponse = await fetch(UPSTREAM_WEBHOOK_URL, {
      method: "POST",
      headers: upstreamHeaders,
      body: rawBody,
    });

    const responseBody = await upstreamResponse.text();
    const responseHeaders = new Headers();
    const upstreamContentType = upstreamResponse.headers.get("content-type");
    responseHeaders.set("Content-Type", upstreamContentType || "application/json");

    return new Response(responseBody, {
      status: upstreamResponse.status,
      statusText: upstreamResponse.statusText,
      headers: responseHeaders,
    });
  } catch (upstreamError) {
    // Return sanitized 502 Bad Gateway without exposing stack traces or payload details
    return new Response(
      JSON.stringify({
        outcome: "upstream_error",
        apiStatus: 502,
        detail: "Upstream lead intake service unavailable.",
      }),
      {
        status: 502,
        headers: {
          "Content-Type": "application/json",
        },
      }
    );
  }
}

// 1. Cloudflare Pages Functions interface
export async function onRequest(context) {
  return handleWebhookProxy(context.request);
}

// 2. Cloudflare Workers interface (for wrangler deploy / Workers Builds)
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/webhook/lead-intake" || url.pathname === "/webhook/lead-intake/") {
      return handleWebhookProxy(request);
    }
    // Fall back to static assets if env.ASSETS is bound
    if (env && env.ASSETS && typeof env.ASSETS.fetch === "function") {
      return env.ASSETS.fetch(request);
    }
    return new Response("Not Found", { status: 404 });
  },
};
