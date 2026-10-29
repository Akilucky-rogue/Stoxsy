-- Migration 049: NSE derivatives end-of-day archive (the official F&O bhavcopy), the F&O research layer's historical source.
--
-- One row per listed contract per session as NSE published it (futures and options on indices and stocks; every strike). Two file formats feed it: the legacy
-- `fo<DD><MON><YYYY>bhav.csv.zip` (through 2024-07-05) and the UDiFF `BhavCopy_NSE_FO_0_0_0_<yyyymmdd>_F_0000.csv.zip` (from the overlap in mid-2024 on). Raw files are landed first.
-- To bound size, contracts with neither open interest nor volume on a session are NOT stored; `fno_eod_sessions` records how many rows each file held and how many were dropped,
-- so coverage is auditable. Implied volatility is not published: it is derived later, in a labelled research step.
BEGIN;

CREATE TABLE IF NOT EXISTS fno_eod_raw (
    trade_date        DATE         NOT NULL,
    instrument        TEXT         NOT NULL CHECK (instrument IN ('FUTIDX', 'FUTSTK', 'OPTIDX', 'OPTSTK')),
    symbol            VARCHAR(30)  NOT NULL,
    expiry_date       DATE         NOT NULL,
    strike            NUMERIC(14,2) NOT NULL DEFAULT 0,          -- 0 for futures
    option_type       VARCHAR(3)   NOT NULL CHECK (option_type IN ('FUT', 'CE', 'PE')),
    open              DOUBLE PRECISION,
    high              DOUBLE PRECISION,
    low               DOUBLE PRECISION,
    close             DOUBLE PRECISION,
    settle            DOUBLE PRECISION,
    last              DOUBLE PRECISION,                           -- UDiFF only
    prev_close        DOUBLE PRECISION,                           -- UDiFF only
    underlying_price  DOUBLE PRECISION,                           -- UDiFF only
    contracts         BIGINT,                                     -- volume in contracts
    value_inr         DOUBLE PRECISION,                           -- traded value in rupees (legacy lakh x 1e5)
    open_interest     BIGINT,
    chg_oi            BIGINT,
    trades            BIGINT,                                     -- UDiFF only
    lot_size          INTEGER,                                    -- UDiFF only
    fin_instrm_id     BIGINT,                                     -- UDiFF only
    source_format     TEXT         NOT NULL,                      -- legacy | udiff
    source_file       TEXT,
    known_at          TIMESTAMPTZ  NOT NULL,
    loaded_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (trade_date, instrument, symbol, expiry_date, strike, option_type)
);
SELECT create_hypertable('fno_eod_raw', 'trade_date', chunk_time_interval => INTERVAL '3 months', if_not_exists => TRUE, migrate_data => TRUE);
CREATE INDEX IF NOT EXISTS idx_fno_symbol_date ON fno_eod_raw (symbol, trade_date);
CREATE INDEX IF NOT EXISTS idx_fno_symbol_expiry ON fno_eod_raw (symbol, expiry_date, trade_date);

-- compression keeps the archive small; skipped quietly if the extension build does not support it
DO $$
BEGIN
    ALTER TABLE fno_eod_raw SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol, instrument', timescaledb.compress_orderby = 'trade_date DESC');
    PERFORM add_compression_policy('fno_eod_raw', INTERVAL '90 days', if_not_exists => TRUE);
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'compression not enabled for fno_eod_raw: %', SQLERRM;
END $$;

CREATE TABLE IF NOT EXISTS fno_eod_sessions (
    trade_date     DATE        PRIMARY KEY,
    source_format  TEXT        NOT NULL,
    sha256         CHAR(64)    NOT NULL,
    n_rows_file    INTEGER     NOT NULL,
    n_rows_kept    INTEGER     NOT NULL,
    n_dropped_idle INTEGER     NOT NULL,
    n_other_types  INTEGER     NOT NULL DEFAULT 0,                -- rows of an instrument type this table does not hold
    known_at       TIMESTAMPTZ NOT NULL,
    loaded_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE fno_eod_raw IS 'NSE F&O bhavcopy as published (raw prices, no adjustment). Contracts with no OI and no volume are not stored; see fno_eod_sessions.';
COMMIT;
