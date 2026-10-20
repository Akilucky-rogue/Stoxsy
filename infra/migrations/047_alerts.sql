-- Migration 047: in-app alerts. Alerts are DERIVED state: each evaluation recomputes which conditions hold, opens an alert for a new condition, refreshes one that persists,
-- and resolves one whose condition has cleared. An owner acknowledgement is kept across refreshes. Alerts never place, change or cancel anything.
-- user_id is NULL for system-wide alerts (data, sources, models) and the signed-in user's id (TEXT: local mode uses 'local') for portfolio alerts.
BEGIN;

CREATE TABLE IF NOT EXISTS alert_events (
    id            BIGSERIAL   PRIMARY KEY,
    user_id       TEXT,
    rule_id       TEXT        NOT NULL,
    severity      TEXT        NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    title         TEXT        NOT NULL,
    detail        TEXT        NOT NULL,
    entity_type   TEXT,
    entity_id     TEXT,
    dedup_key     TEXT        NOT NULL,
    state         TEXT        NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'acknowledged', 'resolved')),
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    occurrences   INTEGER     NOT NULL DEFAULT 1,
    resolved_at   TIMESTAMPTZ,
    resolved_why  TEXT,
    acked_by      TEXT,
    acked_at      TIMESTAMPTZ,
    payload       JSONB       NOT NULL DEFAULT '{}'::jsonb
);
-- at most one live (open / acknowledged) alert per condition and scope
CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_live ON alert_events (COALESCE(user_id, ''), dedup_key) WHERE state <> 'resolved';
CREATE INDEX IF NOT EXISTS idx_alert_state ON alert_events (state, severity, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_alert_user ON alert_events (user_id, state);

CREATE TABLE IF NOT EXISTS alert_runs (
    id           BIGSERIAL   PRIMARY KEY,
    scope        TEXT        NOT NULL,                  -- 'global' or 'user:<id>'
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    n_conditions INTEGER,
    opened       INTEGER,
    resolved     INTEGER,
    errors       JSONB       NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_alert_runs ON alert_runs (scope, started_at DESC);
COMMIT;
