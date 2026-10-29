-- Migration 041: NSE equity master (ISINs for every NSE-listed equity) and review status on the NSE <-> BSE cross-listing map.
--
-- A link is only usable (link_status = 'linked_verified') when the ISIN agrees AND the two exchanges' closing prices agree on the same days
-- (they are the same security, so they must). A pair whose median close gap exceeds 1%, or that shows any day above 10%, is
-- 'unlinked_pending_review': neither side may be stitched, aggregated or used in a combined series until a person verifies it.
BEGIN;

CREATE TABLE IF NOT EXISTS nse_equity_master (
    symbol         VARCHAR(30)  PRIMARY KEY,
    name           TEXT,
    series         VARCHAR(8),
    listing_date   DATE,
    isin           VARCHAR(12)  NOT NULL,
    face_value     DOUBLE PRECISION,
    source_sha256  CHAR(64)     NOT NULL,
    known_at       TIMESTAMPTZ  NOT NULL,
    loaded_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_nse_master_isin ON nse_equity_master (isin);

ALTER TABLE listing_xref ADD COLUMN IF NOT EXISTS link_status   TEXT NOT NULL DEFAULT 'linked_unchecked'
    CHECK (link_status IN ('linked_verified', 'linked_unchecked', 'unlinked_pending_review'));
ALTER TABLE listing_xref ADD COLUMN IF NOT EXISTS review_reason TEXT;
ALTER TABLE listing_xref ADD COLUMN IF NOT EXISTS overlap_days  INTEGER;
ALTER TABLE listing_xref ADD COLUMN IF NOT EXISTS median_gap    DOUBLE PRECISION;
ALTER TABLE listing_xref ADD COLUMN IF NOT EXISTS max_gap       DOUBLE PRECISION;
ALTER TABLE listing_xref ADD COLUMN IF NOT EXISTS review_rank   INTEGER;     -- 1 = review first (liquid, in the NSE top-500, conflicts)

CREATE TABLE IF NOT EXISTS xref_conflicts (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT        NOT NULL,     -- nse_symbol_many_isins | nse_isin_many_symbols | bse_isin_many_scrips | master_vs_history_isin
    isin            VARCHAR(12),
    nse_symbol      VARCHAR(30),
    bse_scrip_code  INTEGER,
    detail          JSONB,
    built_by        TEXT        NOT NULL,
    built_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE bse_eod_raw IS 'BSE bhavcopy as published: raw_prices_only (NO corporate-action adjustment; BSE-only names must not enter the universe, signals or cards).';
COMMIT;
