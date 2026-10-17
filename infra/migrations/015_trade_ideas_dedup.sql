-- 015_trade_ideas_dedup.sql
--
-- Idempotency for the idea-log (Phase 0 increment 2.5). Re-running an engine
-- scan the same day must NOT create duplicate ideas — that would double-count
-- the Scorecard, the exact integrity failure the Scorecard exists to prevent.
-- One idea per (engine, symbol, day, structure), mirroring paper_trades'
-- UNIQUE(symbol, horizon, entry_date).

-- 1. The calendar day of the idea, backfilled from created_at.
ALTER TABLE trade_ideas ADD COLUMN IF NOT EXISTS idea_date DATE;
UPDATE trade_ideas SET idea_date = created_at::date WHERE idea_date IS NULL;

-- 2. Collapse any pre-existing duplicates, keeping the EARLIEST row per key
--    ((created_at, idea_id) tiebreak guarantees exactly one survivor).
DELETE FROM trade_ideas t
 USING trade_ideas d
 WHERE t.engine    = d.engine
   AND t.symbol    = d.symbol
   AND t.idea_date = d.idea_date
   AND t.structure = d.structure
   AND (t.created_at > d.created_at
        OR (t.created_at = d.created_at AND t.idea_id > d.idea_id));

-- 3. Enforce it (idea_date is always set by record_idea going forward).
ALTER TABLE trade_ideas ALTER COLUMN idea_date SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_ideas_engine_symbol_day_structure
    ON trade_ideas (engine, symbol, idea_date, structure);
