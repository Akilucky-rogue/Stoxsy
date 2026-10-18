-- Migration 036: registry for reviewed price-adjustment materializations (split/bonus-adjusted prices, total-return series).
--
-- The materializations themselves are immutable Parquet under data/adjusted/<kind>/<version>/ with a MANIFEST.json; these tables
-- record what was built from which inputs, which events were applied, and whether the build passed validation. They never replace
-- or modify `ohlcv`. A series with validation_status <> 'validated' must not be used for training.
BEGIN;

CREATE TABLE IF NOT EXISTS adjustment_runs (
    version            TEXT PRIMARY KEY,                 -- e.g. sbadj_v1-3fa9c1d2e4
    kind               TEXT NOT NULL CHECK (kind IN ('split_bonus', 'total_return')),
    built_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    inputs             JSONB NOT NULL,                   -- fingerprints: event-set hash, raw-price fingerprint, approvals sha, dependency
    output_hash        CHAR(64) NOT NULL,                -- sha256 over the output files; identical inputs MUST give an identical hash
    rows               BIGINT NOT NULL,
    n_events           INT,
    validation_status  TEXT NOT NULL DEFAULT 'built' CHECK (validation_status IN ('built', 'validated', 'failed')),
    validation         JSONB,
    path               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS adjustment_events_applied (
    id               BIGSERIAL PRIMARY KEY,
    version          TEXT NOT NULL REFERENCES adjustment_runs(version) ON DELETE CASCADE,
    symbol           VARCHAR(20) NOT NULL,               -- the symbol that traded (follows official renames)
    security_id      TEXT,
    gap_date         DATE NOT NULL,                      -- first bar after the action: bars strictly before it are scaled
    nse_ex_date      DATE,
    factor           DOUBLE PRECISION NOT NULL,          -- the NSE-listed price multiplier, not the observed gap
    tier             TEXT NOT NULL,                      -- confirmed | official_confirmed | owner_approved
    source           TEXT NOT NULL,                      -- ca_reconcile_v1 | owner_approval
    source_record_id BIGINT,                             -- nse_corporate_actions.id when available
    isin             TEXT,
    evidence_ref     TEXT,
    UNIQUE (version, symbol, gap_date, factor)
);
CREATE INDEX IF NOT EXISTS idx_adj_events_symbol ON adjustment_events_applied (symbol, gap_date);

COMMENT ON TABLE adjustment_runs IS 'Reviewed adjustment materializations; validation_status must be validated before any model uses a series.';

COMMIT;
