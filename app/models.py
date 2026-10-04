from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

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

    score: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1)


LeadStatus = Literal["new", "contacted", "qualified", "closed"]


class LeadStatusUpdate(BaseModel):
    status: LeadStatus
