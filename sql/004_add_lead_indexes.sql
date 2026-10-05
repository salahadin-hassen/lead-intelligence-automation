-- Pre-n8n hardening: indexes for the dominant list/filter/export queries.
--
-- GET /leads, GET /leads/export and the COUNT that backs `total` all share
-- one shape in app/db.py, with `status` and `source` each independently
-- optional:
--
--     WHERE status = ... AND source = ...
--     ORDER BY created_at DESC, id DESC
--
-- a) (created_at DESC, id DESC) is exactly that ORDER BY key, filtered or
--    not, so rows come back already ordered: the planner can drop the Sort
--    node and stop walking as soon as LIMIT is satisfied. Chosen over a
--    (status, ...) or (source, ...) index first because it serves every
--    list/export call, and the unfiltered default view is the hot path.
--    Measured with EXPLAIN ANALYZE on 100k synthetic rows:
--      unfiltered list    41 ms  -> 0.05 ms
--      offset=9000 page   53 ms  -> 7 ms
--      ?source= list     5.3 ms  -> 0.7 ms
--
-- b) (status, created_at DESC, id DESC) is the classic equality-column-first,
--    sort-columns-last composite for the status-workflow views
--    (?status=new|contacted|qualified|closed, list and export). Nothing
--    currently leads with `status`, so those calls fall back to a full scan
--    plus sort. Measured: ?status= list 19-37 ms -> <0.1 ms,
--    export?status=new 94 ms -> 59 ms.
--
-- `source` is deliberately NOT indexed: the existing
-- UNIQUE (source, external_id) constraint already gives the planner a
-- source-leading access path (5.3 ms at baseline, 0.7 ms once (a) exists),
-- so a third index would be near-redundant write overhead. Likewise no
-- single-column `status` index: (b) subsumes it.
--
-- Idempotent by construction: init_schema() re-applies every file in sql/
-- on each startup and the test schema fixture applies them once per session,
-- hence CREATE INDEX IF NOT EXISTS. Never CONCURRENTLY - it cannot run
-- inside those transactions, and the table is small at that point.

CREATE INDEX IF NOT EXISTS leads_created_at_id_idx
    ON leads (created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS leads_status_created_at_id_idx
    ON leads (status, created_at DESC, id DESC);
