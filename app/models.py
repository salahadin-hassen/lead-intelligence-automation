from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

REQUIRED_NON_EMPTY_FIELDS = ("name", "company", "message", "source")


class LeadCreate(BaseModel):
    name: str
    email: EmailStr
    company: str
    message: str
    source: str
    external_id: str | None = None

    @field_validator(*REQUIRED_NON_EMPTY_FIELDS)
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped


class LeadResponse(BaseModel):
    lead_id: int
    name: str
    email: EmailStr
    company: str
    source: str
    status: str
    created_at: datetime
    score: int | None = None
    score_reason: str | None = None
    scored_at: datetime | None = None


class LeadListResponse(BaseModel):
    items: list[LeadResponse]
    total: int
    limit: int
    offset: int


class ScoringResult(BaseModel):
    """Strict shape expected from the lead-scoring model reply."""

    score: int = Field(strict=True, ge=0, le=100)
    reason: str = Field(min_length=1)


IntentCategory = Literal["quote_request", "consultation", "support", "partnership", "other"]
Urgency = Literal["low", "medium", "high"]


class AiInsights(BaseModel):
    """Strict shape required from the AI insights model reply.

    Free-text fields are length-capped by app.insights before validation
    (strict Structured Outputs cannot express maxLength).
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1)
    intent_category: IntentCategory
    urgency: Urgency
    recommended_next_action: str = Field(min_length=1)
    reason: str = Field(min_length=1)


AiInsightsStatus = Literal["not_requested", "disabled", "pending", "completed", "failed"]


class LeadInsightsResponse(BaseModel):
    """AI insights for one lead. Supplementary only: never changes the score."""

    lead_id: int
    status: AiInsightsStatus
    insights: AiInsights | None = None
    model: str | None = None
    error: str | None = None
    usage: dict[str, int] | None = None
    updated_at: datetime | None = None
    cached: bool = False


LeadStatus = Literal["new", "contacted", "qualified", "closed"]


class LeadStatusUpdate(BaseModel):
    status: LeadStatus
