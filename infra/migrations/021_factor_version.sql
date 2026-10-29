-- Migration 021: add factor_version column to factor_pit (C1 fix).
--
-- Background (AUDIT_v2 C1):
--   Models were trained on FRACTIONALLY-DIFFERENCED features (dataset.py
--   default apply_fracdiff=True → frac_diff_ffd(d=0.4, ~150-bar kernel)).
--   Serving in signal_generator.py read the RAW latest values from
--   factor_pit and called model.predict() directly, applying no transform.
--   Every level-form feature (OBV, rolling-VWAP levels, volume-profile POC,
--   SMA/EMA levels, Donchian levels, macro levels) sat in completely
--   different units at serve time vs train time. Tree splits learned on
--   fracdiff magnitudes routed raw levels into degenerate leaves. Every
--   production ML signal was systematically distorted.
--
-- Fix (audit option "a"):
--   Move fracdiff INTO the factor engine — factor_pit stores the
--   transformed values, tagged with `factor_version = 'ffd_v1'`. Train and
--   serve both read `WHERE factor_version = 'ffd_v1'`. Same values at both
--   ends, no more skew.
--
--   Existing rows in factor_pit were written raw and are tagged 'raw_v0'
--   by this migration. Retraining (Stage 2C.3) triggers a full factor
--   recompute + rewrite with fracdiff applied and `factor_version='ffd_v1'`.
--   The old 'raw_v0' rows can be dropped once the ffd_v1 set is complete.

BEGIN;

-- Add the version column. Default 'raw_v0' so existing rows are honestly
-- tagged as pre-fix data.
ALTER TABLE factor_pit
  ADD COLUMN IF NOT EXISTS factor_version VARCHAR(20) NOT NULL DEFAULT 'raw_v0';

-- Fast lookup for readers filtering by version.
CREATE INDEX IF NOT EXISTS idx_factor_pit_version
    ON factor_pit(factor_version, symbol, as_of_date);

COMMENT ON COLUMN factor_pit.factor_version IS
  'Which factor pipeline version wrote this row. raw_v0 = pre-fix (levels), '
  'ffd_v1 = post-C1-fix (fracdiff d=0.4). Train and serve MUST filter to '
  'the same version — see AUDIT_v2 C1 / migration 021.';

COMMIT;
