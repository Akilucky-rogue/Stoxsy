-- Migration 027: one entry convention for harness, paper journal and Scorecard (ADR 0001).
--
-- Decision at the close of t, fill at the OPEN of t+1, barriers re-anchored to the fill with the signal's
-- fractional offsets (qsde.ml.barrier.levels_at_fill). Stored so every resolved outcome says what it filled at.
BEGIN;

ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS ref_price NUMERIC;           -- signal-time close the levels were set from
ALTER TABLE trade_ideas  ADD COLUMN IF NOT EXISTS entry_fill_price NUMERIC;
ALTER TABLE trade_ideas  ADD COLUMN IF NOT EXISTS entry_fill_date  DATE;

COMMENT ON COLUMN paper_trades.ref_price IS
  'Signal-time reference close. fill_pending_entries rescales stop/target by open/ref_price so offsets match the harness.';
COMMENT ON COLUMN trade_ideas.entry IS
  'Signal-time reference price (close of the decision day). The Scorecard fills at the next session open: see entry_fill_price.';

COMMIT;
