from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.config import get_settings
from app.routes.leads import router as leads_router


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
    return {"status": "ok"}
