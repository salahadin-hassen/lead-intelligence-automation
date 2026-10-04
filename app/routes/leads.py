from fastapi import APIRouter, HTTPException, Query, status

from app import db
from app.errors import DuplicateLeadError
from app.models import LeadCreate, LeadListResponse, LeadResponse

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
    )


@router.post("/leads", response_model=LeadResponse, status_code=status.HTTP_201_CREATED)
def create_lead(lead: LeadCreate) -> LeadResponse:
    try:
        row = db.create_lead(lead)
    except DuplicateLeadError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A lead with this source and external_id already exists",
        ) from exc
    return LeadResponse(
        lead_id=row["id"],
        name=lead.name,
        email=lead.email,
        company=lead.company,
        source=lead.source,
        status=row["status"],
        created_at=row["created_at"],
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
