"""Migration 004: indexes for the list/filter/export query patterns.

Verifies the migration applied, created the intended columns in the intended
order (the ORDER BY tie-break depends on `id`), and can be re-applied —
init_schema() runs every file in sql/ on every application start.
"""

from pathlib import Path

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATION = PROJECT_ROOT / "sql" / "004_add_lead_indexes.sql"


def test_expected_indexes_exist_and_match_query_order(schema_ready: str) -> None:
    with psycopg.connect(schema_ready, autocommit=True) as conn:
        definitions = dict(
            conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s",
                ("leads",),
            ).fetchall()
        )

    assert "(created_at DESC, id DESC)" in definitions["leads_created_at_id_idx"]
    assert (
        "(status, created_at DESC, id DESC)"
        in definitions["leads_status_created_at_id_idx"]
    )


def test_migration_is_idempotent(schema_ready: str) -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    with psycopg.connect(schema_ready, autocommit=True) as conn:
        conn.execute(sql)
        conn.execute(sql)  # re-applying on the next startup must not raise
