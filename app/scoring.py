"""AI lead scoring (Milestone 3).

Provider-specific code lives only in this module. Every AI-layer failure —
missing API key, timeout, HTTP error, malformed reply — degrades to ``None``
so that intake never fails because of scoring. The API key is never printed,
logged, or returned by any endpoint.

Database errors are the database layer's concern and are never caught here.
"""

import re
from typing import Callable

import httpx
from pydantic import ValidationError

from app.config import get_settings
from app.models import LeadCreate, ScoringResult

SCORING_TIMEOUT_SECONDS = 5.0
DEFAULT_BASE_URL = "https://api.openai.com/v1"

SYSTEM_PROMPT = (
    "You are a B2B lead scoring assistant. Score how likely the lead is a "
    "qualified sales opportunity. Respond with ONLY a JSON object of the form "
    '{"score": <integer 0-100>, "reason": "<one short sentence>"} '
    "and no other text."
)

# Callable shape used by the route's dependency injection.
Scorer = Callable[[LeadCreate], ScoringResult | None]


def build_user_message(lead: LeadCreate) -> str:
    """Prompt input is limited to message/company/source to minimize PII."""
    return f"Source: {lead.source}\nCompany: {lead.company}\nMessage: {lead.message}"


def parse_scoring_reply(content: object) -> ScoringResult | None:
    """Parse the model's reply; anything invalid becomes None."""
    if not isinstance(content, str):
        return None
    try:
        return ScoringResult.model_validate_json(content)
    except ValidationError:
        return None


# --- Offline heuristic scorer (Milestone 4) ---------------------------------
# Pure, deterministic function of the lead's fields: no randomness, no clock,
# no I/O, no secrets. Exact weights below are the spec's rule table.

_BASE_SCORE = 45
_KEYWORD_CAP = 30
MAX_REASON_LENGTH = 280

_INTENT_KEYWORDS = {
    "demo": 12,
    "pricing": 12,
    "quote": 10,
    "trial": 10,
    "pilot": 8,
    "enterprise": 8,
    "integration": 8,
    "api": 8,
    "urgent": 6,
    "purchase": 6,
}

_FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "yahoo.co.uk",
        "aol.com",
        "icloud.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "mail.com",
        "yandex.com",
    }
)


def score_lead_heuristic(lead: LeadCreate) -> ScoringResult:
    """Score a lead locally; always returns 0-100 with a readable reason."""
    score = _BASE_SCORE
    reasons: list[str] = []

    matched = [
        keyword
        for keyword in _INTENT_KEYWORDS
        if re.search(rf"\b{re.escape(keyword)}\b", lead.message, re.IGNORECASE)
    ]
    if matched:
        score += min(sum(_INTENT_KEYWORDS[k] for k in matched), _KEYWORD_CAP)
        reasons.append(f"High-intent keywords ({', '.join(matched[:5])})")

    if len(lead.message) >= 100:
        score += 10
        reasons.append("substantial message")
    elif len(lead.message) < 20:
        score -= 15
        reasons.append("very short message")

    domain = str(lead.email).rsplit("@", 1)[-1].lower()
    if domain in _FREE_MAIL_DOMAINS:
        score -= 10
        reasons.append("free-mail email domain")
    else:
        score += 10
        reasons.append("corporate email domain")

    source = lead.source.lower()
    if "referral" in source:
        score += 10
        reasons.append("strong source (referral)")
    elif "contact" in source:
        score += 8
        reasons.append("strong source (contact form)")
    elif "social" in source:
        score -= 5
        reasons.append("low-intent source (social)")
    elif "newsletter" in source:
        score -= 8
        reasons.append("low-intent source (newsletter)")

    score = max(0, min(100, score))
    reason = "; ".join(reasons) if reasons else "Baseline inbound lead"
    return ScoringResult(score=score, reason=reason[:MAX_REASON_LENGTH])


def score(lead: LeadCreate) -> ScoringResult | None:
    """Dispatcher: LLM scorer when a key is configured, heuristic otherwise.

    An LLM failure with a key set stays None (no heuristic fallback), as
    specified in Milestone 4.
    """
    if get_settings().openai_api_key:
        return score_lead(lead)
    return score_lead_heuristic(lead)


def score_lead(lead: LeadCreate) -> ScoringResult | None:
    """Score one lead. Returns None whenever scoring cannot be completed."""
    settings = get_settings()
    api_key = settings.openai_api_key
    if not api_key:
        return None
    base_url = (settings.openai_base_url or DEFAULT_BASE_URL).rstrip("/")
    try:
        response = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": settings.lead_scoring_model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_message(lead)},
                ],
            },
            timeout=SCORING_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return None
    return parse_scoring_reply(content)
