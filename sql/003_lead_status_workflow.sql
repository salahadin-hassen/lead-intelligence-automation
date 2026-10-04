-- Milestone 5: closed lead-status vocabulary.
-- Applied in sorted order after 001/002; idempotent so that lifespan schema
-- init can run on every application start. The database is the authoritative
-- guard against invalid statuses (same pattern as 001's unique constraint).

ALTER TABLE leads DROP CONSTRAINT IF EXISTS leads_status_valid;
ALTER TABLE leads
    ADD CONSTRAINT leads_status_valid
    CHECK (status IN ('new', 'contacted', 'qualified', 'closed'));
