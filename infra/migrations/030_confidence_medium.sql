-- Migration 030: events may carry confidence 'medium' (owner-approved but with a documented weakness, e.g. untraded sessions
-- before the gap bar). 'medium' events are still in the official series when applied; only price-only events are 'low'.
BEGIN;
ALTER TABLE corp_action_events DROP CONSTRAINT IF EXISTS corp_action_events_confidence_check;
ALTER TABLE corp_action_events ADD CONSTRAINT corp_action_events_confidence_check CHECK (confidence IN ('high', 'medium', 'low'));
COMMIT;
