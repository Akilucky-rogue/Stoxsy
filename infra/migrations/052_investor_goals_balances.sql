-- Migration 052: Investor mode inputs: questionnaire answers on the profile, goals, and manual asset-class balances.
-- Goals and balances belong to the INVESTMENT pot only. Amounts live only here (per user), never in the repository. user_id is TEXT (local principal 'local').
BEGIN;

ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS income_stability TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS dependents INTEGER;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS drawdown_reaction TEXT;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'user_profiles_income_stability_check') THEN
        ALTER TABLE user_profiles ADD CONSTRAINT user_profiles_income_stability_check CHECK (income_stability IS NULL OR income_stability IN ('stable', 'variable', 'uncertain'));
        ALTER TABLE user_profiles ADD CONSTRAINT user_profiles_dependents_check CHECK (dependents IS NULL OR dependents >= 0);
        ALTER TABLE user_profiles ADD CONSTRAINT user_profiles_drawdown_reaction_check CHECK (drawdown_reaction IS NULL OR drawdown_reaction IN ('add', 'hold', 'sell_some', 'sell_all'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS goals (
    id             BIGSERIAL    PRIMARY KEY,
    user_id        TEXT         NOT NULL,
    name           TEXT         NOT NULL,
    kind           TEXT         NOT NULL DEFAULT 'goal' CHECK (kind IN ('goal', 'emergency')),
    target_amount  NUMERIC(18,2) NOT NULL CHECK (target_amount > 0),
    target_date    DATE         NOT NULL,
    current_amount NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK (current_amount >= 0),
    monthly_sip    NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK (monthly_sip >= 0),
    priority       INTEGER      NOT NULL DEFAULT 1,
    is_test        BOOLEAN      NOT NULL DEFAULT FALSE,
    archived       BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_goals_user ON goals (user_id, archived, priority);

CREATE TABLE IF NOT EXISTS investor_balances (
    id          BIGSERIAL    PRIMARY KEY,
    user_id     TEXT         NOT NULL,
    asset_class TEXT         NOT NULL CHECK (asset_class IN ('equity', 'gold', 'debt')),
    label       TEXT         NOT NULL,
    amount      NUMERIC(18,2) NOT NULL CHECK (amount >= 0),
    as_of       DATE         NOT NULL,
    source      TEXT         NOT NULL DEFAULT 'manual',
    archived    BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_investor_balances_user ON investor_balances (user_id, archived);

COMMIT;
