-- Migration 054: NSE bulk and block deals from NSE's own daily files. The files are ROLLING (only the latest session is online), so history can only be captured by
-- landing them every day: raw bytes first (data/landing/nse_deals), then these rows. `known_at` is when we received the file; the deal date is the trade date.
BEGIN;
CREATE TABLE IF NOT EXISTS deals_raw (
    kind          TEXT          NOT NULL CHECK (kind IN ('bulk', 'block')),
    trade_date    DATE          NOT NULL,
    symbol        TEXT          NOT NULL,
    security_name TEXT,
    client_name   TEXT          NOT NULL,
    side          TEXT          NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity      BIGINT        NOT NULL CHECK (quantity > 0),
    price         DOUBLE PRECISION NOT NULL CHECK (price > 0),
    remarks       TEXT,
    seq           INTEGER       NOT NULL DEFAULT 0,       -- ordinal among otherwise identical rows of the same file (a client can repeat a trade)
    source_sha256 CHAR(64)      NOT NULL,
    known_at      TIMESTAMPTZ   NOT NULL,
    loaded_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (kind, trade_date, symbol, client_name, side, quantity, price, seq)
);
CREATE INDEX IF NOT EXISTS idx_deals_raw_symbol ON deals_raw (symbol, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_deals_raw_date ON deals_raw (trade_date DESC);
COMMIT;
