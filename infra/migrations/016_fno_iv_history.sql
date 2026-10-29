-- 016_fno_iv_history.sql
--
-- Daily ATM implied-volatility history per index/expiry (F&O increment 4).
--
-- The FnoIncomeEngine sells premium ONLY when vol is RICH — measured by
-- iv_rank, which is "where does today's ATM IV sit inside its own trailing
-- range?". That question is unanswerable without a persisted history, so this
-- table IS the iv_rank denominator. One row per (underlying, as_of_date,
-- expiry), written each session by the chain snapshotter.
--
-- Lookahead discipline: iv_rank for a given day reads ONLY rows with
-- as_of_date STRICTLY BEFORE that day (see iv_store.load_atm_iv_history), so a
-- backtest can never rank today's IV against a window that contains today.

CREATE TABLE IF NOT EXISTS fno_iv_history (
    underlying   VARCHAR(24)  NOT NULL,        -- 'NIFTY' | 'BANKNIFTY' | ...
    as_of_date   DATE         NOT NULL,        -- the session this IV was observed
    expiry       DATE         NOT NULL,        -- the expiry the ATM IV is measured on
    spot         FLOAT8,                        -- underlying price used for the ATM pick
    atm_strike   FLOAT8,
    atm_iv       FLOAT8       NOT NULL,        -- mean(CE_iv, PE_iv) at the ATM strike
    t_years      FLOAT8,
    source       VARCHAR(24),                   -- 'bhavcopy' | 'kite'
    fetched_at   TIMESTAMPTZ  DEFAULT NOW(),

    PRIMARY KEY (underlying, as_of_date, expiry)
);

-- Trailing-window reads are keyed by (underlying, as_of_date).
CREATE INDEX IF NOT EXISTS idx_fno_iv_hist_underlying_date
    ON fno_iv_history (underlying, as_of_date DESC);
