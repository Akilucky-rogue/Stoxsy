-- Migration 024: registry of Parquet feature snapshots (ADR 0001).
--
-- The feature matrix lives in Parquet under STOXSY_FEATURE_DIR; each snapshot
-- directory carries its own MANIFEST.json (self-describing, authoritative for
-- content). This table is the governance layer: what exists, and which
-- snapshots are CURRENT. `is_current` lives here and never in the files.
--   kind = 'full'       supersedes every earlier snapshot of the same
--                       (factor_version, universe_version)
--   kind = 'increment'  adds rows; where (symbol,date) overlap, the newest
--                       built_at wins in the reader.
BEGIN;

CREATE TABLE IF NOT EXISTS feature_snapshots (
    snapshot_id      TEXT        PRIMARY KEY,
    factor_version   TEXT        NOT NULL,
    universe_version TEXT        NOT NULL,
    kind             TEXT        NOT NULL CHECK (kind IN ('full', 'increment')),
    built_at         TIMESTAMPTZ NOT NULL,
    dataset_hash     TEXT        NOT NULL,
    n_rows           BIGINT      NOT NULL,
    n_symbols        INTEGER     NOT NULL,
    date_min         DATE,
    date_max         DATE,
    path             TEXT        NOT NULL,
    git_sha          TEXT,
    manifest         JSONB       NOT NULL,
    is_current       BOOLEAN     NOT NULL DEFAULT TRUE,
    registered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_feature_snapshots_ver
    ON feature_snapshots (factor_version, universe_version, built_at DESC);

COMMIT;
