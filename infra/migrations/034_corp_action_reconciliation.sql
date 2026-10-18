-- Migration 034: reconciliation of NSE-published corporate actions against the price archive and the security map.
--
-- One row per (nse_corporate_actions row, reconciler version). NOTHING here changes any price: this is the evidence layer a
-- separate, reviewed adjustment pipeline will read. classification:
--   confirmed     the action is identified AND the price record agrees (split/bonus: gap within the hard bounds and persistent;
--                 dividend: bars exist and the amount is sane; rights: ratio and premium parsed and bars exist)
--   needs_review  something does not fit (gap present but outside bounds, unparseable terms, demerger/scheme, ambiguous symbol)
--   rejected      contradicted or unusable (no price gap at all for a listed split/bonus; no price bars for the symbol)
BEGIN;

CREATE TABLE IF NOT EXISTS corp_action_reconciliation (
    id                 BIGSERIAL PRIMARY KEY,
    action_id          BIGINT      NOT NULL REFERENCES nse_corporate_actions(id) ON DELETE CASCADE,
    reconciler_version TEXT        NOT NULL,
    category           TEXT        NOT NULL,
    symbol             VARCHAR(20) NOT NULL,       -- as keyed by NSE (today's symbol)
    symbol_of_day      VARCHAR(20),                -- the symbol that traded on the ex-date (follows official renames)
    security_id        TEXT,
    ex_date            DATE        NOT NULL,
    classification     TEXT        NOT NULL CHECK (classification IN ('confirmed', 'needs_review', 'rejected')),
    reason             TEXT,
    listed_factor      DOUBLE PRECISION,           -- price multiplier implied by the terms (split/bonus/consolidation)
    observed_ratio     DOUBLE PRECISION,           -- open on the gap bar / last traded close
    factor_error       DOUBLE PRECISION,
    gap_date           DATE,
    shift_sessions     INT,
    untraded_sessions  INT,
    amount             DOUBLE PRECISION,           -- dividend: Rs per share; rights: premium
    yield_pct          DOUBLE PRECISION,           -- dividend / last close
    terms              TEXT,                       -- parsed ratio for rights / bonus / split
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (action_id, reconciler_version)
);
CREATE INDEX IF NOT EXISTS idx_car_class ON corp_action_reconciliation (category, classification);
CREATE INDEX IF NOT EXISTS idx_car_symbol ON corp_action_reconciliation (symbol_of_day, ex_date);

COMMENT ON TABLE corp_action_reconciliation IS
  'Evidence for each NSE corporate action (confirmed / needs_review / rejected). Never applied to prices automatically.';

COMMIT;
