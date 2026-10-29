-- Migration 056: the review state of a corporate-action event, carried on the derived events table and into the official series' lineage.
--   owner_confirmed      the owner accepted the listed factor despite a price residual
--   applied_unreviewed   the listed factor is applied under the owner's policy while evidence is still pending (NOT validated)
--   adjustment_removed   the owner rejected the listed factor: the official series shows the raw price move
-- Also a registry of retained `ohlcv` versions: before any change to the official series a full snapshot is written and recorded here.
BEGIN;
ALTER TABLE corp_action_events ADD COLUMN IF NOT EXISTS review_status TEXT;
ALTER TABLE corp_action_events ADD COLUMN IF NOT EXISTS review_note TEXT;

CREATE TABLE IF NOT EXISTS ohlcv_versions (
    version_id        TEXT        PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason            TEXT        NOT NULL,
    events_fingerprint TEXT       NOT NULL,
    n_rows            BIGINT      NOT NULL,
    n_symbols         INTEGER     NOT NULL,
    date_min          DATE,
    date_max          DATE,
    sha256            CHAR(64)    NOT NULL,
    path              TEXT        NOT NULL,
    policy            JSONB       NOT NULL DEFAULT '{}'::jsonb
);
COMMIT;
