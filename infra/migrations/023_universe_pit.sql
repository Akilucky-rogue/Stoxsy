-- Migration 023: point-in-time tradable universe.
--
-- One row per (date, symbol) that was in the top-N most liquid equities AS OF
-- that date (trailing-window median traded value), so a backtest on date T only
-- sees names that were liquid at T. Replaces the fixed, survivor-only symbol
-- list (AUDIT H6). Built by qsde/universe/pit.py from eod_raw.
BEGIN;

CREATE TABLE IF NOT EXISTS universe_pit (
    date      DATE        NOT NULL,
    symbol    VARCHAR(30) NOT NULL,
    rank      INTEGER     NOT NULL,       -- 1 = most liquid on that date
    adv_cr    FLOAT8      NOT NULL,       -- trailing median daily traded value, Rs crore
    close     FLOAT8,                     -- raw close that day (eligibility check)
    top_n     INTEGER     NOT NULL,       -- the N this row was built with
    method    TEXT        NOT NULL,       -- e.g. 'adv60_top500_v1'
    PRIMARY KEY (date, symbol, method)
);
SELECT create_hypertable('universe_pit', 'date', if_not_exists => TRUE, migrate_data => TRUE);
CREATE INDEX IF NOT EXISTS idx_universe_pit_symbol ON universe_pit (symbol, date);

COMMIT;
