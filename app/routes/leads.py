import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app import db, scoring
from app.errors import DuplicateLeadError
from app.models import LeadCreate, LeadListResponse, LeadResponse, ScoringResult

logger = logging.getLogger(__name__)

# The scorer is injected via FastAPI dependency injection so tests can
# replace it without touching the network or any real credentials.
Scorer = Callable[[LeadCreate], ScoringResult | None]


def get_scorer() -> Scorer:
    return scoring.score_lead


router = APIRouter()


def _to_response(row: dict) -> LeadResponse:
    return LeadResponse(
        lead_id=row["id"],
        name=row["name"],
        email=row["email"],
        company=row["company"],
        source=row["source"],
        status=row["status"],
        created_at=row["created_at"],
        score=row["score"],
        score_reason=row["score_reason"],
        scored_at=row["scored_at"],
    )


@router.post("/leads", response_model=LeadResponse, status_code=status.HTTP_201_CREATED)
def create_lead(lead: LeadCreate, scorer: Scorer = Depends(get_scorer)) -> LeadResponse:
    try:
        row = db.create_lead(lead)
    except DuplicateLeadError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A lead with this source and external_id already exists",
        ) from exc

    lead_id = row["id"]
    score = None
    score_reason = None
    scored_at = None
    try:
        result = scorer(lead)
    except Exception:
        # Intake must never fail because of scoring; log without secrets.
        logger.exception("Lead scoring failed; continuing without a score")
        result = None
    if result is not None:
        # Database errors propagate: only AI-layer failures degrade to NULL.
        scored_at = db.set_lead_score(lead_id, result.score, result.reason)
        score = result.score
        score_reason = result.reason

    return LeadResponse(
        lead_id=lead_id,
        name=lead.name,
        email=lead.email,
        company=lead.company,
        source=lead.source,
        status=row["status"],
        created_at=row["created_at"],
        score=score,
        score_reason=score_reason,
        scored_at=scored_at,
    )


@router.get("/leads", response_model=LeadListResponse)
def list_leads(
    status: str | None = None,
    source: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> LeadListResponse:
    items, total = db.list_leads(status=status, source=source, limit=limit, offset=offset)
    return LeadListResponse(
        items=[_to_response(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/leads/{lead_id}", response_model=LeadResponse)
def get_lead(lead_id: int) -> LeadResponse:
    row = db.get_lead(lead_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found",
        )
    return _to_response(row)
