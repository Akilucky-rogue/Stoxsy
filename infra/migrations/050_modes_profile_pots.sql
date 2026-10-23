-- Migration 050: Trader/Investor mode, user profile, capital partition (pots) and a mode audit trail.
-- user_id is TEXT (the unenforced local mode principal is 'local', which cannot go in audit_log.user_id UUID).
-- Rupee amounts live ONLY here, per user; nothing in the repo carries personal amounts.
BEGIN;

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id            TEXT        PRIMARY KEY,
    mode               TEXT        NOT NULL DEFAULT 'investor' CHECK (mode IN ('investor', 'trader')),
    risk_profile       TEXT        CHECK (risk_profile IN ('conservative', 'moderately_conservative', 'balanced', 'growth', 'aggressive')),
    capacity_score     NUMERIC(5,2),
    tolerance_score    NUMERIC(5,2),
    investment_capital NUMERIC(18,2) CHECK (investment_capital IS NULL OR investment_capital >= 0),
    trading_capital    NUMERIC(18,2) CHECK (trading_capital IS NULL OR trading_capital >= 0),
    emergency_fund     NUMERIC(18,2) CHECK (emergency_fund IS NULL OR emergency_fund >= 0),
    limits             JSONB       NOT NULL DEFAULT '{}'::jsonb,   -- only deviations from the policy defaults
    policy_version     TEXT        NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Every holdings import belongs to exactly one pot. Existing rows stay NULL ("unassigned"): they are NOT silently called investments.
ALTER TABLE portfolio_imports ADD COLUMN IF NOT EXISTS pot TEXT;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'portfolio_imports_pot_check') THEN
        ALTER TABLE portfolio_imports ADD CONSTRAINT portfolio_imports_pot_check CHECK (pot IS NULL OR pot IN ('investment', 'trading'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_portfolio_imports_user_pot ON portfolio_imports (user_id, pot, imported_at DESC);

-- A pot can be set once (NULL -> value) and never changed: a position cannot migrate between pots by an UPDATE.
CREATE OR REPLACE FUNCTION portfolio_imports_pot_immutable() RETURNS trigger AS $$
BEGIN
    IF OLD.pot IS NOT NULL AND NEW.pot IS DISTINCT FROM OLD.pot THEN
        RAISE EXCEPTION 'portfolio_imports.pot is immutable once assigned (% -> %); an explicit audited transfer is required', OLD.pot, NEW.pot;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_portfolio_imports_pot_immutable ON portfolio_imports;
CREATE TRIGGER trg_portfolio_imports_pot_immutable BEFORE UPDATE ON portfolio_imports
    FOR EACH ROW EXECUTE FUNCTION portfolio_imports_pot_immutable();

-- Append-only record of mode changes, capital changes, limit changes and approvals, pot assignments and imports.
CREATE TABLE IF NOT EXISTS mode_audit (
    id         BIGSERIAL   PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id    TEXT        NOT NULL,
    actor_role TEXT,
    action     TEXT        NOT NULL,
    payload    JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_mode_audit_user ON mode_audit (user_id, ts DESC);

CREATE OR REPLACE FUNCTION mode_audit_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'mode_audit is append-only';
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_mode_audit_immutable ON mode_audit;
CREATE TRIGGER trg_mode_audit_immutable BEFORE UPDATE OR DELETE ON mode_audit
    FOR EACH ROW EXECUTE FUNCTION mode_audit_immutable();

COMMIT;
