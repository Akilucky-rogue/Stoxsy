-- Migration 035: the `official_confirmed` approval tier (owner decision 2026-10-18).
--
-- An NSE-published record that matches the action type, ratio and ex-date, with the ex-date on the gap bar or inside the symbol's
-- untraded window and the observed factor inside the existing 5% / 3% tolerance, may promote a held action. It never overrides a
-- hard timing or factor failure. Events approved this way carry their evidence: ISIN, source URL, retrieval time, evidence-pack ref.
BEGIN;
ALTER TABLE corp_action_events DROP CONSTRAINT IF EXISTS corp_action_events_confidence_check;
ALTER TABLE corp_action_events ADD CONSTRAINT corp_action_events_confidence_check
    CHECK (confidence IN ('high', 'medium', 'low', 'official_confirmed'));
ALTER TABLE corp_action_events ADD COLUMN IF NOT EXISTS approval_tier TEXT;
ALTER TABLE corp_action_events ADD COLUMN IF NOT EXISTS isin          TEXT;
ALTER TABLE corp_action_events ADD COLUMN IF NOT EXISTS source_url    TEXT;
ALTER TABLE corp_action_events ADD COLUMN IF NOT EXISTS retrieved_at  TIMESTAMPTZ;
ALTER TABLE corp_action_events ADD COLUMN IF NOT EXISTS evidence_ref  TEXT;
COMMIT;
