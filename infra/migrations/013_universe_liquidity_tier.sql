-- 013_universe_liquidity_tier.sql
--
-- Add ADV-based liquidity classification to the universe table so the
-- Tier 1 rule engine can filter by 'tradeable' tier instead of computing
-- ADV inline on every signal generation run. Nightly expand_universe_*
-- script writes this column; engine reads it.
--
-- Tier definitions (set in qsde/universe/liquidity_filter.py):
--   tradeable: trailing-60d avg daily value traded >= ₹10cr/day
--   marginal:  ₹1cr <= ADV < ₹10cr (we ingest data but flag as not tradeable)
--   illiquid:  ADV < ₹1cr (we may ingest for ground-truth but never trade)
--
-- Idempotent. Safe to re-run.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'universe' AND column_name = 'liquidity_tier'
    ) THEN
        ALTER TABLE universe
            ADD COLUMN liquidity_tier VARCHAR(20),
            ADD COLUMN adv_60d        FLOAT8,
            ADD COLUMN tier_computed_at TIMESTAMPTZ;
    END IF;
END $$;

-- Index for the engine's "give me all tradeable symbols" query.
CREATE INDEX IF NOT EXISTS idx_universe_liquidity_tier
    ON universe(liquidity_tier) WHERE is_active = TRUE;
