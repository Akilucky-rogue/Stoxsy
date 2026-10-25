-- Migration 058: legacy triage for positions imported into the TRADING pot. Existing book first: an imported trading-pot holding carries no stop, so the owner records one here
-- (and optionally a horizon) before any new idea is offered. The pot itself is already fixed per import (migration 050, immutable); this table only adds the protection data.
-- Rows are upserted by (import_id, symbol); every change is also written to mode_audit by the API.
BEGIN;
CREATE TABLE IF NOT EXISTS legacy_triage (
    import_id   BIGINT       NOT NULL REFERENCES portfolio_imports (id) ON DELETE CASCADE,
    symbol      TEXT         NOT NULL,
    user_id     TEXT         NOT NULL,
    stop_price  NUMERIC(14,4) NOT NULL CHECK (stop_price > 0),
    horizon_id  TEXT,
    set_by_role TEXT         NOT NULL,
    set_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (import_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_legacy_triage_user ON legacy_triage (user_id);
COMMIT;
