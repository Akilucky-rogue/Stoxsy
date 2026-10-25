-- Migration 059: official exchange trading-holiday calendar and a derived F&O expiry calendar.
-- exchange_holidays: one row per exchange, segment and date, landed raw-first from NSE's holiday master (the sha256 of the landed file is kept). A date listed here is a non-session even on a weekday;
-- weekends are non-sessions without needing a row. `special_session` marks entries NSE stars as special-timing days (Muhurat trading on Diwali): they are NOT regular full sessions and are never counted as one.
-- The published list covers one calendar year at a time, so coverage ends on 31 December of the latest year loaded; after that, dates are estimates and every consumer must say so.
-- fno_expiry_calendar: the expiries that actually exist in the F&O archive (holiday-shifted dates are exact because they come from the contracts themselves), for the latest archived session.
BEGIN;
CREATE TABLE IF NOT EXISTS exchange_holidays (
    exchange       TEXT        NOT NULL,
    segment        TEXT        NOT NULL,
    holiday_date   DATE        NOT NULL,
    weekday        TEXT        NOT NULL,
    description    TEXT        NOT NULL,
    special_session BOOLEAN    NOT NULL DEFAULT FALSE,
    source_sha256  CHAR(64)    NOT NULL,
    known_at       TIMESTAMPTZ NOT NULL,
    loaded_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (exchange, segment, holiday_date)
);
CREATE INDEX IF NOT EXISTS idx_exchange_holidays_date ON exchange_holidays (holiday_date);

CREATE OR REPLACE VIEW fno_expiry_calendar AS
SELECT symbol, instrument, expiry_date, count(*) AS n_contracts, max(lot_size) AS lot_size
FROM fno_eod_raw
WHERE trade_date = (SELECT max(trade_date) FROM fno_eod_raw) AND expiry_date >= (SELECT max(trade_date) FROM fno_eod_raw)
GROUP BY symbol, instrument, expiry_date;
COMMIT;
