"""PostgreSQL access layer.

All SQL and psycopg-specific exceptions stay inside this module. The API
route only sees application-level results and errors (e.g.
:class:`app.errors.DuplicateLeadError`).
"""

from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
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

_UPDATE_LEAD_STATUS_SQL = f"""
    UPDATE leads
    SET status = %(status)s
    WHERE id = %(id)s
    RETURNING {_LEAD_COLUMNS}
"""

# Export needs every column, including message/external_id which the JSON API
# deliberately omits; column order matches the CSV header order.
_EXPORT_COLUMNS = (
    "id, name, email, company, message, source, external_id, status, "
    "score, score_reason, scored_at, created_at"
)

_EXPORT_LEADS_SQL = f"""
    SELECT {_EXPORT_COLUMNS}
    FROM leads
    WHERE (%(status)s::text IS NULL OR status = %(status)s)
      AND (%(source)s::text IS NULL OR source = %(source)s)
    ORDER BY created_at DESC, id DESC
"""

_DELETE_LEAD_SQL = """
    DELETE FROM leads
    WHERE id = %s
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


# /health must answer promptly even when the pool is saturated: the pool's
# default wait is 30 s. Set to 5.0s to allow for serverless cold-start wakeups.
HEALTH_PING_TIMEOUT_SECONDS = 5.0


def ping() -> None:
    """Verify the pool can reach PostgreSQL by running the cheapest query.

    Used by GET /health so the endpoint distinguishes "process is up" from
    "the database is actually reachable". Runs on the existing pool; no
    separate connection mechanism.

    Raises:
        RuntimeError: if the pool has not been configured.
        psycopg.Error / psycopg_pool.PoolTimeout: if the database is
            unreachable or no connection frees up in time.
    """
    with _get_pool().connection(timeout=HEALTH_PING_TIMEOUT_SECONDS) as conn:
        conn.execute("SELECT 1")


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


def update_lead_status(lead_id: int, status_value: str) -> dict | None:
    """Update a lead's status and return the updated row.

    Returns None when no lead with that id exists (mapped to 404 by the route).
    """
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                _UPDATE_LEAD_STATUS_SQL, {"status": status_value, "id": lead_id}
            ).fetchone()
    return dict(row) if row is not None else None


def export_leads(status: str | None, source: str | None) -> list[dict]:
    """Return every matching lead with all columns, newest first."""
    params = {"status": status, "source": source}
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(_EXPORT_LEADS_SQL, params).fetchall()
    return [dict(row) for row in rows]


_GET_LEAD_INSIGHTS_SQL = """
    SELECT id, company, message, source, ai_status, ai_insights, ai_model, ai_error, ai_usage, ai_updated_at
    FROM leads
    WHERE id = %s
"""

# Takes the one-call-per-lead claim. Never-requested leads are claimable; a
# failed attempt only with retry; a stale 'pending' (process died mid-call)
# after %(stale_seconds)s. 'completed' is never claimable.
_CLAIM_LEAD_INSIGHTS_SQL = """
    UPDATE leads
    SET ai_status = 'pending', ai_error = NULL, ai_updated_at = CURRENT_TIMESTAMP
    WHERE id = %(id)s
      AND (ai_status IS NULL
           OR (ai_status = 'failed' AND %(retry)s)
           OR (ai_status = 'pending'
               AND ai_updated_at < CURRENT_TIMESTAMP - make_interval(secs => %(stale_seconds)s)))
    RETURNING id
"""

# Counts one paid call in the current fixed hour/day window, but only while the
# window is below its limit; no row returned means the limit is reached. The
# conflict update row-locks the window, so concurrent reservations serialize.
_RESERVE_BUDGET_SQL = """
    INSERT INTO ai_insights_budget (window_kind, window_start, calls)
    VALUES (%(kind)s, date_trunc(%(kind)s, CURRENT_TIMESTAMP), 1)
    ON CONFLICT (window_kind, window_start)
    DO UPDATE SET calls = ai_insights_budget.calls + 1
    WHERE ai_insights_budget.calls < %(limit)s
    RETURNING calls
"""

_FINISH_LEAD_INSIGHTS_SQL = """
    UPDATE leads
    SET ai_status = %(status)s, ai_insights = %(insights)s, ai_model = %(model)s,
        ai_error = %(error)s, ai_usage = %(usage)s, ai_updated_at = CURRENT_TIMESTAMP
    WHERE id = %(id)s AND ai_status = 'pending'
    RETURNING ai_status, ai_insights, ai_model, ai_error, ai_usage, ai_updated_at
"""


def get_lead_insights(lead_id: int) -> dict | None:
    """Return the AI-relevant fields and stored insights state of one lead."""
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(_GET_LEAD_INSIGHTS_SQL, (lead_id,)).fetchone()
    return dict(row) if row is not None else None


def reserve_lead_insights(
    lead_id: int,
    *,
    retry: bool,
    stale_seconds: float,
    hourly_limit: int,
    daily_limit: int,
) -> str:
    """Claim the lead AND one unit of the global call budget, atomically.

    Returns "reserved" (the caller may make exactly one model call),
    "not_claimable" (completed, in progress, or failed without retry; no
    budget used) or "budget_exceeded" (nothing changed; the lead stays
    claimable for later). Database errors propagate, so a broken budget check
    can never lead to a model call.
    """
    params = {"id": lead_id, "retry": retry, "stale_seconds": stale_seconds}
    with _get_pool().connection() as conn:
        with conn.transaction():
            if conn.execute(_CLAIM_LEAD_INSIGHTS_SQL, params).fetchone() is None:
                return "not_claimable"
            for kind, limit in (("hour", hourly_limit), ("day", daily_limit)):
                reserved = limit > 0 and conn.execute(
                    _RESERVE_BUDGET_SQL, {"kind": kind, "limit": limit}
                ).fetchone() is not None
                if not reserved:
                    # Undo the claim and any window already counted.
                    raise psycopg.Rollback()
            return "reserved"
    return "budget_exceeded"


def finish_lead_insights(
    lead_id: int,
    *,
    status: str,
    insights: dict | None,
    model: str | None,
    error: str | None,
    usage: dict | None = None,
) -> dict | None:
    """Store the outcome of a claimed request; None if the claim was lost."""
    params = {
        "id": lead_id,
        "status": status,
        "insights": Jsonb(insights) if insights is not None else None,
        "model": model,
        "error": error,
        "usage": Jsonb(usage) if usage is not None else None,
    }
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(_FINISH_LEAD_INSIGHTS_SQL, params).fetchone()
    return dict(row) if row is not None else None


def delete_lead(lead_id: int) -> bool:
    """Delete one lead; True when a row was removed, False when not found."""
    with _get_pool().connection() as conn:
        deleted = conn.execute(_DELETE_LEAD_SQL, (lead_id,)).rowcount
    return deleted > 0
