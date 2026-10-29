-- Migration 031: per-source health for continuous data (docs/STREAMING_DATA.md). Health is data: a silent feed must show up here.
BEGIN;
CREATE TABLE IF NOT EXISTS source_health (
    source               TEXT PRIMARY KEY,
    status               TEXT NOT NULL DEFAULT 'unknown' CHECK (status IN ('unknown','ok','failing','stale','parse_failed')),
    last_attempt_at      TIMESTAMPTZ,
    last_success_at      TIMESTAMPTZ,
    last_data_time       TIMESTAMPTZ,
    consecutive_failures INT NOT NULL DEFAULT 0,
    last_error           TEXT,
    rows_last            BIGINT NOT NULL DEFAULT 0,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE source_health IS 'One row per ingestion source; written by qsde.stream runner/scheduler, read by the Data & System page and alert rules.';
COMMIT;
