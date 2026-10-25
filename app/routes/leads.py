import csv
import io
import logging
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import db, insights, scoring
from app.config import Settings, get_settings
from app.errors import DuplicateLeadError
from app.insights import InsightsGenerator
from app.models import (
    AiInsights,
    LeadCreate,
    LeadInsightsResponse,
    LeadListResponse,
    LeadResponse,
    LeadStatusUpdate,
    ScoringResult,
)

logger = logging.getLogger(__name__)

# The scorer is injected via FastAPI dependency injection so tests can
# replace it without touching the network or any real credentials.
Scorer = Callable[[LeadCreate], ScoringResult | None]


def get_scorer() -> Scorer:
    return scoring.score


_bearer_security = HTTPBearer(auto_error=False)


def verify_api_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_security),
    settings: Settings = Depends(get_settings),
) -> None:
    # Fail-closed in production: unconfigured token or missing credentials always rejected
    if settings.is_production:
        if not settings.api_auth_token:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Authentication misconfigured in production",
            )
        if credentials is None or credentials.credentials != settings.api_auth_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return

    # In development / testing:
    # If a token is explicitly configured, enforce it; otherwise permit for testing.
    if settings.api_auth_token:
        if credentials is None or credentials.credentials != settings.api_auth_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )


router = APIRouter(dependencies=[Depends(verify_api_token)])


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


EXPORT_FILENAME = "leads-export.csv"
EXPORT_COLUMNS = [
    "id",
    "name",
    "email",
    "company",
    "message",
    "source",
    "external_id",
    "status",
    "score",
    "score_reason",
    "scored_at",
    "created_at",
]
_DATETIME_COLUMNS = ("scored_at", "created_at")


def _csv_ready_row(row: dict) -> dict:
    out = dict(row)
    for key in _DATETIME_COLUMNS:
        value = out.get(key)
        if isinstance(value, datetime):
            out[key] = value.isoformat()
    return out


def _build_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(_csv_ready_row(row))
    return buffer.getvalue()


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
    # Named status_filter so it does not shadow the imported fastapi.status
    # module; the wire-level query parameter stays `?status=...`.
    status_filter: str | None = Query(default=None, alias="status"),
    source: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> LeadListResponse:
    items, total = db.list_leads(
        status=status_filter, source=source, limit=limit, offset=offset
    )
    return LeadListResponse(
        items=[_to_response(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


# Must be registered before /leads/{lead_id}, otherwise the path parameter
# would swallow "export" and answer 422.
@router.get("/leads/export")
def export_leads(
    # Same alias trick as list_leads: local name avoids shadowing
    # fastapi.status while `?status=...` keeps working unchanged.
    status_filter: str | None = Query(default=None, alias="status"),
    source: str | None = None,
) -> Response:
    rows = db.export_leads(status=status_filter, source=source)
    return Response(
        content=_build_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{EXPORT_FILENAME}"'},
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


def get_insights_generator() -> InsightsGenerator:
    return insights.generate_insights


def _insights_response(lead_id: int, row: dict, *, cached: bool) -> LeadInsightsResponse:
    stored = row.get("ai_insights")
    return LeadInsightsResponse(
        lead_id=lead_id,
        status=row.get("ai_status") or "not_requested",
        # Stored JSON was validated before it was written; validate again on read.
        insights=AiInsights.model_validate(stored) if stored else None,
        model=row.get("ai_model"),
        error=row.get("ai_error"),
        usage=row.get("ai_usage"),
        updated_at=row.get("ai_updated_at"),
        cached=cached,
    )


@router.get("/leads/{lead_id}/insights", response_model=LeadInsightsResponse)
def get_lead_insights(lead_id: int) -> LeadInsightsResponse:
    """Read stored AI insights; never calls the model."""
    row = db.get_lead_insights(lead_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")
    return _insights_response(lead_id, row, cached=True)


@router.post("/leads/{lead_id}/insights", response_model=LeadInsightsResponse)
def create_lead_insights(
    lead_id: int,
    retry: bool = False,
    settings: Settings = Depends(get_settings),
    generator: InsightsGenerator = Depends(get_insights_generator),
) -> LeadInsightsResponse:
    """Generate AI insights once per lead (stored results are returned as-is).

    Every handled outcome answers 200 with a status: disabled, pending,
    completed or failed (with an error code). A failed attempt is only
    repeated with ?retry=true. The deterministic score is never touched.
    """
    row = db.get_lead_insights(lead_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")
    if row.get("ai_status") == "completed":
        return _insights_response(lead_id, row, cached=True)
    if not settings.ai_insights_enabled or not settings.openai_api_key:
        return LeadInsightsResponse(
            lead_id=lead_id,
            status="disabled",
            error=None if not settings.ai_insights_enabled else "not_configured",
        )

    # The claim and one unit of the global hourly/daily call budget are taken
    # in one transaction: this is the only way to reach the paid model call,
    # whatever path (website, direct webhook, demo flag) created the lead.
    reservation = db.reserve_lead_insights(
        lead_id,
        retry=retry,
        stale_seconds=settings.ai_insights_timeout_seconds + 60,
        hourly_limit=settings.ai_insights_max_calls_per_hour,
        daily_limit=settings.ai_insights_max_calls_per_day,
    )
    if reservation == "budget_exceeded":
        # Not persisted: no call was made and the lead stays claimable.
        logger.warning("AI insights call budget reached; lead %s not enriched", lead_id)
        return LeadInsightsResponse(lead_id=lead_id, status="failed", error="budget_exceeded")
    if reservation != "reserved":
        return _insights_response(lead_id, db.get_lead_insights(lead_id) or row, cached=True)

    try:
        outcome = generator(row, settings)
    except Exception:
        # The generator handles provider errors; this only guards the claim.
        logger.exception("AI insights generator failed unexpectedly")
        outcome = insights.InsightsOutcome(
            insights=None, error="internal_error", model=settings.ai_insights_model
        )
    finished = db.finish_lead_insights(
        lead_id,
        status="completed" if outcome.insights is not None else "failed",
        insights=outcome.insights.model_dump() if outcome.insights is not None else None,
        model=outcome.model,
        error=outcome.error,
        usage=outcome.usage,
    )
    return _insights_response(lead_id, finished or db.get_lead_insights(lead_id) or row, cached=False)


@router.patch("/leads/{lead_id}", response_model=LeadResponse)
def update_lead_status(lead_id: int, update: LeadStatusUpdate) -> LeadResponse:
    row = db.update_lead_status(lead_id, update.status)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found",
        )
    return _to_response(row)


@router.delete("/leads/{lead_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lead(lead_id: int) -> Response:
    if not db.delete_lead(lead_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
