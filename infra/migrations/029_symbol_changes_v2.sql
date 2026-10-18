-- Migration 029: symbol_changes carries the full authority record (owner spec 2026-10-18).
--   verification_status  'official' (matches the NSE symbol-change list) | 'inferred' (price-archive inference only)
--                        | 'rejected' (contradicted / chance twin).  Only 'official' rows may ever drive stitching.
BEGIN;

ALTER TABLE symbol_changes ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE symbol_changes ADD COLUMN IF NOT EXISTS effective_date DATE;
ALTER TABLE symbol_changes ADD COLUMN IF NOT EXISTS change_type TEXT NOT NULL DEFAULT 'symbol_change';
ALTER TABLE symbol_changes ADD COLUMN IF NOT EXISTS source_url TEXT;
ALTER TABLE symbol_changes ADD COLUMN IF NOT EXISTS retrieved_at TIMESTAMPTZ;
ALTER TABLE symbol_changes ADD COLUMN IF NOT EXISTS verification_status TEXT NOT NULL DEFAULT 'inferred';
ALTER TABLE symbol_changes ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE symbol_changes ALTER COLUMN effective_after DROP NOT NULL;

ALTER TABLE symbol_changes DROP CONSTRAINT IF EXISTS symbol_changes_pkey;
ALTER TABLE symbol_changes ADD PRIMARY KEY (id);
ALTER TABLE symbol_changes DROP CONSTRAINT IF EXISTS symbol_changes_verification_status_check;
ALTER TABLE symbol_changes ADD CONSTRAINT symbol_changes_verification_status_check
    CHECK (verification_status IN ('official', 'inferred', 'rejected'));
CREATE UNIQUE INDEX IF NOT EXISTS uq_symbol_changes
    ON symbol_changes (old_symbol, new_symbol, source, COALESCE(effective_date, DATE '1900-01-01'));
CREATE INDEX IF NOT EXISTS idx_symbol_changes_old ON symbol_changes (old_symbol);
CREATE INDEX IF NOT EXISTS idx_symbol_changes_new ON symbol_changes (new_symbol);

COMMENT ON TABLE symbol_changes IS
  'Old->new NSE symbol map. verification_status=official rows come from the NSE symbol-change list (source_url, retrieved_at); '
  'inferred rows come from the price archive and are never used to alter or stitch prices.';

COMMIT;
