-- Migration 039: BSE daily bhavcopy, kept in its OWN table so no NSE reader (which assumes eod_raw is NSE) can ever see it.
--
-- One row per BSE scrip per session, exactly as the exchange published it (adjustment-free), keyed by the BSE scrip code (the stable BSE id;
-- BSE tickers were only added to the file in 2024). The ISIN is present from 2017-18 onward and is the primary key for the NSE <-> BSE
-- cross-listing map; before that it is NULL and a link needs another source. `known_at` is publication time for backfilled history.
BEGIN;

CREATE TABLE IF NOT EXISTS bse_eod_raw (
    scrip_code      INTEGER      NOT NULL,
    date            DATE         NOT NULL,
    ticker          VARCHAR(30),                 -- TckrSymb (UDiFF files, 2024+); NULL in the older formats
    name            TEXT,
    grp             VARCHAR(4),                  -- BSE group / series: A, B, T, X, XT, Z, M, MT, F, G ...
    isin            VARCHAR(12),
    open            FLOAT8,
    high            FLOAT8,
    low             FLOAT8,
    close           FLOAT8,
    last            FLOAT8,
    prev_close      FLOAT8,
    volume          BIGINT,
    turnover_value  FLOAT8,                      -- Rs
    trades          BIGINT,
    source_format   TEXT         NOT NULL,       -- udiff | eq_isincode | eq_legacy
    source_file     TEXT,
    known_at        TIMESTAMPTZ  NOT NULL,
    loaded_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scrip_code, date)
);
SELECT create_hypertable('bse_eod_raw', 'date', if_not_exists => TRUE, migrate_data => TRUE);
CREATE INDEX IF NOT EXISTS idx_bse_eod_isin ON bse_eod_raw (isin, date);
CREATE INDEX IF NOT EXISTS idx_bse_eod_ticker ON bse_eod_raw (ticker, date);

COMMENT ON TABLE bse_eod_raw IS 'BSE bhavcopy as published (raw, unadjusted). NSE data stays in eod_raw; cross-exchange views join on ISIN.';
COMMIT;
