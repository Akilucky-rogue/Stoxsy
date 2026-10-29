-- Migration 055: liveness of the long-running parts (stream, daily pipeline, scheduler). Each component writes a heartbeat; the alert engine judges staleness.
-- A stream that stops because a machine slept must be visible, not silent.
BEGIN;
CREATE TABLE IF NOT EXISTS ops_heartbeat (
    component  TEXT        PRIMARY KEY,
    last_beat  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status     TEXT        NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'degraded', 'failed', 'stopped')),
    host       TEXT,
    detail     JSONB       NOT NULL DEFAULT '{}'::jsonb
);
COMMIT;
