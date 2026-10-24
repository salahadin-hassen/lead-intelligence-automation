-- Optional AI lead insights (app/insights.py).
-- Applied in sorted order after 001-004; idempotent so that lifespan schema
-- init can run on every application start. Additive and nullable: existing
-- rows and the deterministic score/score_reason columns are untouched, and
-- NULL ai_status means "never requested".
--
-- ai_status 'pending' is an atomic claim taken before the model call, so
-- concurrent or repeated requests for the same lead cannot pay for a second
-- call. ai_insights holds the schema-validated JSON object only; ai_usage the
-- token counts OpenAI reported for that call (real cost evidence).

ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS ai_status     TEXT NULL,
    ADD COLUMN IF NOT EXISTS ai_insights   JSONB NULL,
    ADD COLUMN IF NOT EXISTS ai_model      TEXT NULL,
    ADD COLUMN IF NOT EXISTS ai_error      TEXT NULL,
    ADD COLUMN IF NOT EXISTS ai_usage      JSONB NULL,
    ADD COLUMN IF NOT EXISTS ai_updated_at TIMESTAMPTZ NULL;

ALTER TABLE leads DROP CONSTRAINT IF EXISTS leads_ai_status_valid;
ALTER TABLE leads
    ADD CONSTRAINT leads_ai_status_valid
    CHECK (ai_status IS NULL OR ai_status IN ('pending', 'completed', 'failed'));
