-- Milestone 3: AI lead scoring columns.
-- Applied in sorted order after 001_create_leads.sql; idempotent so that
-- lifespan schema init can run on every application start.

ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS score        INTEGER NULL,
    ADD COLUMN IF NOT EXISTS score_reason TEXT NULL,
    ADD COLUMN IF NOT EXISTS scored_at    TIMESTAMPTZ NULL;

ALTER TABLE leads DROP CONSTRAINT IF EXISTS leads_score_range;
ALTER TABLE leads
    ADD CONSTRAINT leads_score_range
    CHECK (score IS NULL OR (score >= 0 AND score <= 100));
