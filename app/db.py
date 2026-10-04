"""PostgreSQL access layer.

All SQL and psycopg-specific exceptions stay inside this module. The API
route only sees application-level results and errors (e.g.
:class:`app.errors.DuplicateLeadError`).
"""

from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.errors import DuplicateLeadError
from app.models import LeadCreate

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "sql"

_INSERT_LEAD_SQL = """
    INSERT INTO leads (name, email, company, message, source, external_id)
    VALUES (%s, %s, %s, %s, %s, %s)
    RETURNING id, status, created_at
"""

_LEAD_COLUMNS = (
    "id, name, email, company, source, status, created_at, score, score_reason, scored_at"
)

# NULL filters are handled in SQL with (%(x)s::text IS NULL OR col = %(x)s) so
# the query text is static and user values are only ever bound parameters.
# The ::text cast is required: PostgreSQL cannot infer the type of a parameter
# used in IS NULL ("could not determine data type of parameter $1").
_LIST_LEADS_SQL = f"""
    SELECT {_LEAD_COLUMNS}
    FROM leads
    WHERE (%(status)s::text IS NULL OR status = %(status)s)
      AND (%(source)s::text IS NULL OR source = %(source)s)
    ORDER BY created_at DESC, id DESC
    LIMIT %(limit)s OFFSET %(offset)s
"""

_COUNT_LEADS_SQL = """
    SELECT count(*) AS total
    FROM leads
    WHERE (%(status)s::text IS NULL OR status = %(status)s)
      AND (%(source)s::text IS NULL OR source = %(source)s)
"""

_GET_LEAD_SQL = f"""
    SELECT {_LEAD_COLUMNS}
    FROM leads
    WHERE id = %s
"""

_SET_LEAD_SCORE_SQL = """
    UPDATE leads
    SET score = %s, score_reason = %s, scored_at = CURRENT_TIMESTAMP
    WHERE id = %s
    RETURNING scored_at
"""

_pool: ConnectionPool | None = None


def configure_pool(database_url: str) -> None:
    """Create the application connection pool from the configured DATABASE_URL."""
    global _pool
    close_pool()
    _pool = ConnectionPool(conninfo=database_url, min_size=1, max_size=4, open=False)
    _pool.open()


def close_pool() -> None:
    """Close the application connection pool if it is open."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("Database pool is not configured. Call configure_pool() first.")
    return _pool


def init_schema() -> None:
    """Apply every SQL file in sql/ (sorted order, idempotent)."""
    with _get_pool().connection() as conn:
        for path in sorted(SCHEMA_DIR.glob("*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))


def create_lead(lead: LeadCreate) -> dict:
    """Insert a lead and return its database-generated fields.

    Raises:
        DuplicateLeadError: if a lead with the same (source, external_id)
            already exists, translated from the database unique violation.
    """
    params = (
        lead.name,
        str(lead.email),
        lead.company,
        lead.message,
        lead.source,
        lead.external_id,
    )
    try:
        with _get_pool().connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(_INSERT_LEAD_SQL, params).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise DuplicateLeadError(source=lead.source, external_id=lead.external_id) from exc
    if row is None:  # pragma: no cover - INSERT ... RETURNING always yields a row
        raise RuntimeError("INSERT into leads returned no row")
    return dict(row)


def list_leads(
    status: str | None,
    source: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    """Return one page of leads (newest first) and the total matching count."""
    params = {"status": status, "source": source, "limit": limit, "offset": offset}
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            items = cur.execute(_LIST_LEADS_SQL, params).fetchall()
            total = cur.execute(_COUNT_LEADS_SQL, params).fetchone()["total"]
    return [dict(item) for item in items], total


def get_lead(lead_id: int) -> dict | None:
    """Return one lead by id, or None when no such lead exists."""
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(_GET_LEAD_SQL, (lead_id,)).fetchone()
    return dict(row) if row is not None else None


def set_lead_score(lead_id: int, score: int, score_reason: str) -> datetime:
    """Persist a scoring result for a lead and return the scored_at timestamp.

    Raises:
        RuntimeError: if the UPDATE matched no row (lead disappeared).
    """
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(_SET_LEAD_SCORE_SQL, (score, score_reason, lead_id)).fetchone()
    if row is None:
        raise RuntimeError("UPDATE leads ... RETURNING scored_at returned no row")
    return row["scored_at"]
