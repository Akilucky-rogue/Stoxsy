-- Migration 051: after-close reconciliation of streamed minute bars against Kite's own historical minute bars.
-- Streamed bars (ohlcv_intraday) are built from price snapshots and can miss a few paise on a high or low and start mid-minute; Kite's historical bars are
-- the reference. Both are KEPT (the streamed bar is evidence); the view below prefers the official bar wherever one exists.
BEGIN;

CREATE TABLE IF NOT EXISTS ohlcv_intraday_official (
    symbol     VARCHAR(20)  NOT NULL,
    ts         TIMESTAMPTZ  NOT NULL,
    open       FLOAT8       NOT NULL,
    high       FLOAT8       NOT NULL,
    low        FLOAT8       NOT NULL,
    close      FLOAT8       NOT NULL,
    volume     BIGINT       NOT NULL DEFAULT 0,
    source     TEXT         NOT NULL DEFAULT 'kite_historical',
    fetched_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_intraday_official_ts ON ohlcv_intraday_official (ts);

CREATE TABLE IF NOT EXISTS intraday_reconciliation (
    session_date  DATE        NOT NULL,
    symbol        VARCHAR(20) NOT NULL,
    status        TEXT        NOT NULL CHECK (status IN ('ok', 'gappy', 'price_drift', 'volume_off', 'no_official', 'no_stream', 'too_short')),
    n_stream      INTEGER     NOT NULL,
    n_official    INTEGER     NOT NULL,
    n_both        INTEGER     NOT NULL,
    stats         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    reconciled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (session_date, symbol)
);

-- Best available minute bar: the official one where it exists, else the streamed one, with the provenance named.
CREATE OR REPLACE VIEW ohlcv_intraday_best AS
SELECT DISTINCT ON (symbol, ts) symbol, ts, open, high, low, close, volume, source
FROM (
    SELECT symbol, ts, open, high, low, close, volume, 'kite_historical'::text AS source, 1 AS rank FROM ohlcv_intraday_official
    UNION ALL
    SELECT symbol, ts, open, high, low, close, volume, 'streamed'::text AS source, 2 AS rank FROM ohlcv_intraday
) u
ORDER BY symbol, ts, rank;

COMMIT;
