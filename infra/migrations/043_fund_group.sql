-- Migration 043: explicit fund_group (vehicle / strategy) and a reason for every scheme that stays unclassified, plus lifecycle status.
-- Ambiguous schemes are NEVER forced into an investment category: they stay `unclassified` with a stored reason.
BEGIN;
ALTER TABLE mf_scheme_master ADD COLUMN IF NOT EXISTS fund_group TEXT;
ALTER TABLE mf_scheme_master ADD COLUMN IF NOT EXISTS unclassified_reason TEXT;
ALTER TABLE mf_scheme_master ADD COLUMN IF NOT EXISTS lifecycle TEXT;      -- active | recent_launch | inactive_no_recent_nav | single_nav
CREATE INDEX IF NOT EXISTS idx_mf_master_group ON mf_scheme_master (fund_group, asset_class);
COMMIT;
