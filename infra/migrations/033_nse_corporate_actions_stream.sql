-- Migration 033: corporate actions as published by NSE (stream source), with the fields the old feed lacked.
--
-- record_date, face_value and ISIN come straight from the NSE corporate-actions API; dividends, rights, demergers and
-- buy-backs are kept (category + parsed factor where an equity ratio exists). `known_at` is the first time WE saw this exact
-- content; an unchanged record fetched again is NOT re-inserted (content_hash), a corrected record IS a new row.
-- This table is NOT yet a source of the official price adjustment: that is a separate reviewed change.
BEGIN;

CREATE TABLE IF NOT EXISTS nse_corporate_actions (
    id            BIGSERIAL PRIMARY KEY,
    symbol        VARCHAR(20)  NOT NULL,
    series        VARCHAR(8),
    isin          VARCHAR(16),
    company       TEXT,
    subject       TEXT         NOT NULL,
    category      TEXT,                          -- SPLIT_BONUS | CONSOLIDATION | DEMERGER | DIVIDEND | RIGHTS | OTHER
    factor        DOUBLE PRECISION,              -- price multiplier before the ex-date, only for parseable equity ratios
    face_value    DOUBLE PRECISION,
    ex_date       DATE         NOT NULL,
    record_date   DATE,
    event_time    TIMESTAMPTZ  NOT NULL,
    known_at      TIMESTAMPTZ  NOT NULL,
    content_hash  CHAR(64)     NOT NULL,
    source_sha256 CHAR(64),
    UNIQUE (symbol, ex_date, subject, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_nse_ca_symbol_ex ON nse_corporate_actions (symbol, ex_date);
CREATE INDEX IF NOT EXISTS idx_nse_ca_category ON nse_corporate_actions (category, ex_date);

COMMENT ON TABLE nse_corporate_actions IS
  'NSE corporate-actions API rows (record date, face value, ISIN included). Landed raw first (data/landing/nse_corporate_actions). '
  'Not applied to prices by itself.';

COMMIT;
