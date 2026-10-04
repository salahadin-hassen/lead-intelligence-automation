"""Test fixtures.

Database tests fail loudly when the test database is unavailable:
no skips, no xfails.
"""

import os
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = PROJECT_ROOT / "sql"

# Allow TEST_DATABASE_URL from .env without overriding a real environment
# variable. .env itself is never printed.
load_dotenv(PROJECT_ROOT / ".env")


def _require_test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "TEST_DATABASE_URL is not set. Database tests fail loudly by design: "
            "set TEST_DATABASE_URL to the PostgreSQL DSN of the test database "
            "(for example postgresql://lead_app:<password>@localhost:5432/lead_automation_test)."
        )
    return url


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return _require_test_database_url()


@pytest.fixture(scope="session")
def schema_ready(test_database_url: str) -> str:
    """Create a clean schema in the test database before the session runs."""
    try:
        with psycopg.connect(test_database_url, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS leads;")
            for path in sorted(SQL_DIR.glob("*.sql")):
                conn.execute(path.read_text(encoding="utf-8"))
    except psycopg.Error as exc:
        raise RuntimeError(
            "The test PostgreSQL database is unavailable or the schema could not "
            "be applied. Database tests fail loudly by design. "
            f"Underlying driver error: {exc.__class__.__name__}: {exc}"
        ) from exc
    return test_database_url


@pytest.fixture(scope="session")
def client(schema_ready: str):
    """FastAPI test client with the app pointed at the test database."""
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = schema_ready
    get_settings.cache_clear()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        from app import db

        db.close_pool()
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clean_leads(schema_ready: str):
    """Truncate the test table before every test."""
    with psycopg.connect(schema_ready, autocommit=True) as conn:
        conn.execute("TRUNCATE TABLE leads RESTART IDENTITY;")
    yield
