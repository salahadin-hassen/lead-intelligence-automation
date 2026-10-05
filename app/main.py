import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status

from app import db
from app.config import get_settings
from app.routes.leads import router as leads_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db.configure_pool(get_settings().database_url)
    try:
        db.init_schema()
        yield
    finally:
        db.close_pool()


app = FastAPI(title="Lead Intelligence Automation", lifespan=lifespan)
app.include_router(leads_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    """200 only when the process is up AND PostgreSQL is reachable.

    503 means the application is running but cannot reach the database, so
    probes can tell "restart the process" apart from "fix the database".
    The failure body is generic: driver messages can embed the DSN and must
    never be returned or logged.
    """
    try:
        db.ping()
    except Exception as exc:
        logger.warning("Health check failed: database unreachable (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service unavailable",
        ) from exc
    return {"status": "ok"}
