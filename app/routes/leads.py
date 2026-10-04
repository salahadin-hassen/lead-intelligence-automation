from fastapi import APIRouter, HTTPException, status

from app import db
from app.errors import DuplicateLeadError
from app.models import LeadCreate, LeadResponse

router = APIRouter()


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
