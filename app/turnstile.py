"""Server-side Cloudflare Turnstile verification for public lead intake.

The public website form reaches POST /leads through n8n, which holds the API
bearer token, so the bearer token alone cannot tell a browser that solved a
challenge from a script posting to the public webhook. Every website
submission therefore carries a Turnstile token (X-Turnstile-Token), verified
here against Cloudflare's Siteverify endpoint before the lead is validated,
scored or stored.

Fail-closed rules:
- Missing configuration, timeouts, network errors, non-200 replies, malformed
  JSON and provider-side errors are "unavailable" (HTTP 503), never "valid".
- A token is "valid" only when Siteverify answers success=true AND the
  hostname is one of the configured hostnames AND the action matches exactly.

Neither the secret nor the token is ever logged or returned to callers; only
Cloudflare's documented error codes (or our own reason codes) are logged.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import httpx

from app.config import Settings

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
# Cloudflare documents a 2048-character maximum token length.
MAX_TOKEN_LENGTH = 2048
# The only trusted server-to-server channel exempt from Turnstile. Matched
# exactly (case-sensitive) and only after bearer authentication succeeded.
GMAIL_CHANNEL = "gmail"

# Siteverify error codes that mean OUR request or Cloudflare failed, not the
# visitor: verification could not be performed, so the answer is 503.
_PROVIDER_ERROR_CODES = frozenset(
    {"missing-input-secret", "invalid-input-secret", "bad-request", "internal-error"}
)
# Visitor-side failures we may name in logs (anything else is "rejected").
_KNOWN_REJECTION_CODES = frozenset(
    {"missing-input-response", "invalid-input-response", "timeout-or-duplicate"}
)

Outcome = Literal["valid", "rejected", "unavailable"]


@dataclass(frozen=True)
class TurnstileResult:
    outcome: Outcome
    reason: str


TurnstileVerifier = Callable[[str | None, Settings], TurnstileResult]
HttpPost = Callable[..., httpx.Response]


def verify_token(token: str | None, settings: Settings, *, post: HttpPost = httpx.post) -> TurnstileResult:
    """Verify one Turnstile token; `post` is injectable for tests."""
    if not settings.turnstile_secret_key or not settings.turnstile_hostnames or not settings.turnstile_expected_action:
        return TurnstileResult("unavailable", "not_configured")
    if token is None or not token.strip():
        return TurnstileResult("rejected", "missing_token")
    if len(token) > MAX_TOKEN_LENGTH:
        return TurnstileResult("rejected", "token_too_long")

    try:
        response = post(
            SITEVERIFY_URL,
            data={"secret": settings.turnstile_secret_key, "response": token},
            timeout=settings.turnstile_timeout_seconds,
        )
    except httpx.TimeoutException:
        return TurnstileResult("unavailable", "timeout")
    except httpx.HTTPError:
        return TurnstileResult("unavailable", "connection_error")
    except Exception:  # noqa: BLE001 — any other failure must still fail closed
        return TurnstileResult("unavailable", "unexpected_error")

    if response.status_code != 200:
        return TurnstileResult("unavailable", f"http_{response.status_code}")
    try:
        body = response.json()
    except ValueError:
        return TurnstileResult("unavailable", "malformed_response")
    if not isinstance(body, dict) or not isinstance(body.get("success"), bool):
        return TurnstileResult("unavailable", "malformed_response")

    if body["success"] is not True:
        codes = body.get("error-codes")
        codes = [c for c in codes if isinstance(c, str)] if isinstance(codes, list) else []
        provider = sorted(_PROVIDER_ERROR_CODES.intersection(codes))
        if provider:
            return TurnstileResult("unavailable", f"provider_{provider[0]}")
        known = sorted(_KNOWN_REJECTION_CODES.intersection(codes))
        return TurnstileResult("rejected", known[0] if known else "rejected")

    hostname = body.get("hostname")
    if not isinstance(hostname, str) or hostname not in settings.turnstile_hostnames:
        return TurnstileResult("rejected", "hostname_mismatch")
    if body.get("action") != settings.turnstile_expected_action:
        return TurnstileResult("rejected", "action_mismatch")
    return TurnstileResult("valid", "ok")
