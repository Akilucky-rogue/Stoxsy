-- Migration 026: two-tier corporate-action policy + factor_pit deprecation (owner decisions 2026-10-18).
--
--   applied           event is in the OFFICIAL adjusted series (`ohlcv`): NSE-source-confirmed only
--   applied_research  event is also usable by explicit research runs (adds low-confidence price-inferred events)
--   confidence        'high' (NSE listing + price confirmation) | 'low' (price-only)
-- A price-only event never alters the official `ohlcv` that promotion decisions rest on.
BEGIN;

-- The migration runner re-applies every file, so the data backfill must run ONCE (when the columns are created);
-- re-running it later would overwrite applied_research flags written by corp_actions.rebuild_from_db().
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'corp_action_events' AND column_name = 'applied_research') THEN
        ALTER TABLE corp_action_events ADD COLUMN applied_research BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE corp_action_events ADD COLUMN IF NOT EXISTS confidence TEXT NOT NULL DEFAULT 'high';
        UPDATE corp_action_events SET applied_research = applied;
        UPDATE corp_action_events SET confidence = 'low' WHERE source = 'price_inferred';
        UPDATE corp_action_events SET applied = FALSE WHERE source = 'price_inferred';
    END IF;
END $$;

ALTER TABLE corp_action_events DROP CONSTRAINT IF EXISTS corp_action_events_confidence_check;
ALTER TABLE corp_action_events ADD CONSTRAINT corp_action_events_confidence_check
    CHECK (confidence IN ('high', 'medium', 'low', 'official_confirmed'));

COMMENT ON TABLE ohlcv IS
  'Engine input (OFFICIAL series). Adjusted for NSE-source-confirmed split/bonus/consolidation events only '
  '(source tag nse_adj_v2_src); NOT dividend-adjusted; price-inferred events are NOT applied. '
  'Raw prices live in eod_raw; research runs may add low-confidence events via qsde.ml.data.load_prices.';

COMMENT ON TABLE factor_pit IS
  'DEPRECATED 2026-10-18: long-form factor store superseded by the Parquet feature store (ADR 0001). '
  'Kept ONLY as the parity reference for the 46-symbol pilot (8,195,321 ffd_v2 cells, matched exactly). '
  'No live code may read it (enforced by a test). Plan: after the first full-universe snapshot passes validation, '
  'archive/drop it and record its row count and hash in the ADR.';

COMMIT;
