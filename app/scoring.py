"""AI lead scoring (Milestone 3).

Provider-specific code lives only in this module. Every AI-layer failure —
missing API key, timeout, HTTP error, malformed reply — degrades to ``None``
so that intake never fails because of scoring. The API key is never printed,
logged, or returned by any endpoint.

Database errors are the database layer's concern and are never caught here.
"""

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
