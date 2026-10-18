-- Migration 022: raw exchange EOD prices + detected corporate-action events.
--
-- Why: the local NSE bhavcopy archive (2016-2026, whole market, with delivery
-- data) is the price source after the old database was lost. Prices are stored
-- RAW and UNADJUSTED in eod_raw. Corporate-action events inferred from the data
-- are stored separately in corp_action_events, with whether each was applied.
-- `ohlcv` (what the factor engine reads) is built from the two and carries
-- source='nse_bhavcopy_adj_v1'. Adjustment method (documented, see
-- qsde/ingestion/corp_actions.py):
--   * split / bonus / consolidation events with a clean ratio <= 0.80 (or >= 1.4)
--     are applied: prices before the ex-date are multiplied by price_factor,
--     volume divided by it;
--   * mild bonuses (0.80-0.97), rights issues and cash dividends are NOT
--     adjusted (not distinguishable from price moves; unresolved until a
--     reliable corporate-actions source is added), so adj_close == close.
-- Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS eod_raw (
    exchange      VARCHAR(4)  NOT NULL DEFAULT 'NSE',
    symbol        VARCHAR(30) NOT NULL,
    series        VARCHAR(4)  NOT NULL,
    date          DATE        NOT NULL,
    isin          VARCHAR(12),
    open          FLOAT8,
    high          FLOAT8,
    low           FLOAT8,
    close         FLOAT8,
    last          FLOAT8,
    prev_close    FLOAT8,           -- exchange base price; adjusted by NSE on corporate-action ex-dates
    volume        BIGINT,
    turnover_lacs FLOAT8,
    trades        BIGINT,
    deliv_qty     BIGINT,
    deliv_per     FLOAT8,
    source_file   TEXT,
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (exchange, symbol, series, date)
);
SELECT create_hypertable('eod_raw', 'date', if_not_exists => TRUE, migrate_data => TRUE);
CREATE INDEX IF NOT EXISTS idx_eod_raw_symbol_date ON eod_raw (symbol, date);

-- NOTE: PREV_CLOSE in the archived bhavcopy is NOT adjusted on ex-dates (verified:
-- 464 of 465 large drops), so events are inferred from the price gap itself.
CREATE TABLE IF NOT EXISTS corp_action_events (
    id                 BIGSERIAL PRIMARY KEY,
    exchange           VARCHAR(4)  NOT NULL,
    symbol             VARCHAR(30) NOT NULL,
    ex_date            DATE        NOT NULL,
    prev_session       DATE        NOT NULL,
    close_prev_session FLOAT8      NOT NULL,
    open_ex_date       FLOAT8      NOT NULL,
    close_ex_date      FLOAT8      NOT NULL,
    open_ratio         FLOAT8      NOT NULL,   -- open(ex_date) / close(prev_session)
    close_ratio        FLOAT8      NOT NULL,   -- close(ex_date) / close(prev_session)
    volume_ratio       FLOAT8,                 -- volume(ex_date) / volume(prev_session); evidence only
    persistence_ratio  FLOAT8,                 -- median next-3 closes / (prev close x snapped ratio)
    kind               TEXT        NOT NULL
                       CHECK (kind IN ('SPLIT_BONUS','CONSOLIDATION','UNEXPLAINED')),
    snapped_ratio      TEXT,                   -- e.g. '1:2 split' or '1:1 bonus'; NULL if no clean ratio
    price_factor       FLOAT8,                 -- multiplier for prices BEFORE ex_date; NULL when not applied
    applied            BOOLEAN     NOT NULL DEFAULT FALSE,
    method             TEXT        NOT NULL,   -- 'price_gap_fingerprint_v1'
    detected_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (exchange, symbol, ex_date, method)
);
CREATE INDEX IF NOT EXISTS idx_corp_events_symbol ON corp_action_events (symbol, ex_date);

COMMENT ON TABLE ohlcv IS
  'Engine input. Split/bonus/consolidation-adjusted (NOT dividend-adjusted) daily prices; '
  'source column names the method, e.g. nse_bhavcopy_adj_v1. Raw prices live in eod_raw.';

CREATE OR REPLACE VIEW adjusted_ohlcv AS
    SELECT * FROM ohlcv WHERE source LIKE '%\_adj\_%';

COMMIT;
