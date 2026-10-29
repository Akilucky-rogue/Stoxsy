-- Migration 046: per-scheme return / risk / data-quality snapshot (rebuilt after every NAV load by scripts/fund_snapshot.py).
-- A snapshot is derived, never edited: it records the NAV date it was computed as of, so a page can say how fresh it is.
BEGIN;
CREATE TABLE IF NOT EXISTS mf_fund_snapshot (
    scheme_code   INTEGER     PRIMARY KEY REFERENCES mf_scheme_master (scheme_code),
    as_of         DATE        NOT NULL,                 -- the scheme's last NAV date used
    built_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    basis         TEXT        NOT NULL,                 -- total_return_after_expenses | price_return_only | price_return | option_unknown
    returns       JSONB       NOT NULL,                 -- {horizon: {status, cumulative, annualised, start, end | detail}}
    risk          JSONB       NOT NULL,                 -- risk_metrics_detailed over ~3y
    quality       JSONB       NOT NULL,                 -- {level: ok|caution|poor, reasons: [...], flags: {...}}
    peers         JSONB,                                -- {key, n, horizons: {1Y: {percentile, median}}} or NULL when too few peers
    ret_1y        DOUBLE PRECISION,                     -- annualised, NULL unless status ok (sortable copies)
    ret_3y        DOUBLE PRECISION,
    ret_5y        DOUBLE PRECISION,
    vol_3y        DOUBLE PRECISION,
    maxdd_3y      DOUBLE PRECISION,
    quality_level TEXT        NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mf_snapshot_ret1 ON mf_fund_snapshot (ret_1y);
CREATE INDEX IF NOT EXISTS idx_mf_snapshot_q ON mf_fund_snapshot (quality_level);
COMMIT;
