-- Migration 019: paper-trade entries filled at next-session open (H8 fix).
--
-- Background (AUDIT_v2 H8):
--   Paper trades were recorded with entry_price = signal-day close. The EOD
--   job runs after 15:45 IST, so that price is unattainable — you cannot
--   buy today's close after the close. Baselines share the convention, so
--   the model-vs-baseline relative comparison is fair, but the absolute
--   net Sharpe the drift governor escalates on is optimistic by roughly
--   the overnight drift + opening slippage.
--
-- Fix:
--   * entry_price becomes NULLABLE. New rows are inserted with entry_price
--     NULL (pending fill).
--   * entry_fill_date column added. NULL means "not yet filled — waiting
--     for the next session's OHLCV to arrive so we can stamp the open."
--     When fill_pending_entries() runs on session T+1 EOD, it finds these
--     rows and sets entry_price = open of T+1, entry_fill_date = T+1.
--   * reconcile_open_trades skips rows where entry_price IS NULL.
--
-- Backfill:
--   Existing rows in paper_trades have entry_price = signal-day close
--   (the old, optimistic behavior). We tag them as fill_date = entry_date
--   so reconciliation continues to work on them (they behave as before —
--   this is the "grandfathered" set). New rows from this migration onward
--   get the corrected next-open fill semantics.
--
-- Idempotent — running twice is a no-op.

BEGIN;

-- Make entry_price nullable. Postgres allows ALTER COLUMN DROP NOT NULL
-- unconditionally; it's a no-op if already nullable.
ALTER TABLE paper_trades ALTER COLUMN entry_price DROP NOT NULL;

-- Add the fill-date column.
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS entry_fill_date DATE;

-- Backfill existing (grandfathered) rows: their entry_price was the
-- signal-day close, so document that by setting fill_date = entry_date.
-- New rows written by the fixed writer will insert with fill_date NULL
-- until fill_pending_entries() runs.
UPDATE paper_trades
   SET entry_fill_date = entry_date
 WHERE entry_fill_date IS NULL
   AND entry_price     IS NOT NULL;

-- Fast lookup for pending rows.
CREATE INDEX IF NOT EXISTS idx_paper_trades_pending
    ON paper_trades(entry_date)
 WHERE entry_price IS NULL;

COMMENT ON COLUMN paper_trades.entry_fill_date IS
    'Session date at which entry_price was actually attainable. '
    'NULL means the trade is waiting for next-session OHLCV to arrive '
    '(H8 fix). Grandfathered rows carry fill_date = entry_date.';

COMMIT;
