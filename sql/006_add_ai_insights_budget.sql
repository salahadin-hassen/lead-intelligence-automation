-- Global spend ceiling for AI lead insights (app/db.py:reserve_lead_insights).
-- Applied in sorted order after 001-005; idempotent so that lifespan schema
-- init can run on every application start. Additive: no existing table or
-- row is touched.
--
-- The public lead webhook can be called directly (bypassing the website), and
-- every new lead is one paid OpenAI request. No trustworthy client identity
-- reaches the API (proxies hide or overwrite client IPs), so the ceiling is
-- global: one counter row per fixed hour and per fixed day. A call is only
-- made after both counters were incremented below their limits, in the same
-- transaction that claims the lead, so the cap holds under concurrency.

CREATE TABLE IF NOT EXISTS ai_insights_budget (
    window_kind  TEXT        NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    calls        INTEGER     NOT NULL,
    CONSTRAINT ai_insights_budget_pk PRIMARY KEY (window_kind, window_start),
    CONSTRAINT ai_insights_budget_kind_valid CHECK (window_kind IN ('hour', 'day')),
    CONSTRAINT ai_insights_budget_calls_valid CHECK (calls >= 0)
);
