-- Migration 028: symbol-change map (old symbol -> new symbol), with evidence and a verification flag.
--
-- The NSE corporate-action feed uses the CURRENT symbol; eod_raw uses the symbol of the day. A renamed stock is
-- therefore two series until it is stitched under a stable internal security id. Rows start verified = FALSE
-- (price-archive inference); only an NSE symbol-change source or a human may set verified = TRUE. Unverified rows are
-- never used to alter any price series.
BEGIN;

CREATE TABLE IF NOT EXISTS symbol_changes (
    old_symbol      VARCHAR(20)  NOT NULL,
    new_symbol      VARCHAR(20)  NOT NULL,
    effective_after DATE         NOT NULL,          -- last session traded under old_symbol
    status          TEXT         NOT NULL,
    isin_old        TEXT,
    isin_new        TEXT,
    evidence        TEXT,
    source          TEXT         NOT NULL,
    verified        BOOLEAN      NOT NULL DEFAULT FALSE,
    verified_by     TEXT,
    verified_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (old_symbol, new_symbol)
);

COMMENT ON TABLE symbol_changes IS
  'Old->new NSE symbol map. verified=FALSE rows are inferred from the price archive and are NOT used to adjust or stitch prices.';

COMMIT;
