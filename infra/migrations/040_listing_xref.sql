-- Migration 040: one economic security across exchanges. The cross-listing map links an NSE symbol and a BSE scrip code that are the same security,
-- primarily by ISIN. Separate exchange listings (and their separate liquidity) are preserved; this table only says "these listings are one security".
BEGIN;

CREATE TABLE IF NOT EXISTS listing_xref (
    isin            VARCHAR(12),                  -- NULL only for link_basis = 'ticker_price'
    nse_symbol      VARCHAR(30)  NOT NULL,
    bse_scrip_code  INTEGER      NOT NULL,
    link_basis      TEXT         NOT NULL CHECK (link_basis IN ('isin', 'ticker_price')),
    evidence        JSONB,
    built_by        TEXT         NOT NULL,        -- script + version that produced the row
    built_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (nse_symbol, bse_scrip_code)
);
CREATE INDEX IF NOT EXISTS idx_listing_xref_isin ON listing_xref (isin);
CREATE INDEX IF NOT EXISTS idx_listing_xref_bse ON listing_xref (bse_scrip_code);

CREATE TABLE IF NOT EXISTS bse_listing_master (
    scrip_code      INTEGER PRIMARY KEY,
    ticker          VARCHAR(30),
    name            TEXT,
    grp             VARCHAR(4),
    isin            VARCHAR(12),
    first_date      DATE,
    last_date       DATE,
    n_days          INTEGER,
    instrument_type TEXT NOT NULL,                -- equity | fund_etf | debt | other
    type_evidence   TEXT,
    built_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE listing_xref IS 'NSE symbol <-> BSE scrip code links (ISIN first; ticker+price agreement as a labelled weaker tier).';
COMMIT;
