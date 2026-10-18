-- Migration 032: stable security ids across symbol changes + v3 reconciliation candidates.
--
-- security_master    one row per (symbol, validity interval); security_id is stable ('S:' + the earliest symbol in the chain).
--                    Built ONLY from the official NSE symbol-change list and only where the price archive agrees with the
--                    hand-off (old series ends, new series starts, no overlap). Inferred links are never used.
-- corp_action_candidates
--                    what the rename-aware / skipped-session reconciler (source_confirmed_v3) would confirm for listings the
--                    v2 reconciler left SOURCE_UNCONFIRMED. NEVER applied automatically: an event enters the official series
--                    only through an owner approval (infra/data/corp_action_approvals.json) that passes the hard bounds.
BEGIN;

CREATE TABLE IF NOT EXISTS security_master (
    security_id   TEXT        NOT NULL,
    symbol        VARCHAR(20) NOT NULL,
    valid_from    DATE        NOT NULL,
    valid_to      DATE        NOT NULL DEFAULT DATE '9999-12-31',
    source        TEXT        NOT NULL,
    handoff_note  TEXT,
    PRIMARY KEY (symbol, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_security_master_id ON security_master (security_id);

CREATE TABLE IF NOT EXISTS corp_action_candidates (
    id                 BIGSERIAL PRIMARY KEY,
    reconciler_version TEXT        NOT NULL,
    listed_symbol      VARCHAR(20) NOT NULL,       -- the symbol the NSE feed keyed the action under
    symbol_of_day      VARCHAR(20),                -- the symbol that actually traded on the ex-date
    security_id        TEXT,
    listed_ex_date     DATE        NOT NULL,
    gap_date           DATE,
    purpose            TEXT,
    listed_factor      DOUBLE PRECISION,
    observed_ratio     DOUBLE PRECISION,
    factor_error       DOUBLE PRECISION,
    shift_sessions     INT,
    untraded_sessions  INT,
    persistence_ratio  DOUBLE PRECISION,
    classification     TEXT        NOT NULL CHECK (classification IN ('v3_candidate', 'v3_rejected', 'unresolved')),
    reason             TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (reconciler_version, listed_symbol, listed_ex_date)
);

COMMIT;
