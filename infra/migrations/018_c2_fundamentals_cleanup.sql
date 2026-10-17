-- Migration 018: null out lookahead-contaminated fundamentals rows.
--
-- Background (AUDIT_v2 C2):
--   qsde/ingestion/yfinance_client.py::fetch_fundamentals_batch used to
--   build a `trailing` dict from `tk.info` (a CURRENT snapshot of trailing
--   PE, ROE, margins, market cap, etc.) and copy that dict into every
--   historical fiscal-period row. Result: a row dated fiscal_date=2019-03-31
--   carried 2026-era ratios. Every `fund_*` factor over the training
--   history equalled (approximately) its current value.
--
--   The writer has been fixed in Stage 2B.1 to write per-period statement
--   data only (revenue, net_income, fcf, eps) and NULL every ratio /
--   current-snapshot column. Re-running the sync will overwrite existing
--   contaminated rows via ON CONFLICT UPDATE.
--
--   This migration additionally NULLs those columns for any rows that
--   already exist in the DB but WON'T be re-synced (e.g. if a delisted
--   symbol is no longer in the active universe), so no downstream reader
--   can accidentally pick up a poisoned value.
--
--   Idempotent — running twice is a no-op.

BEGIN;

-- Null every column that carried the current-snapshot values. The row
-- itself is preserved (it still has valid per-period revenue / net_income /
-- fcf / eps) so nothing downstream that reads statement data breaks.
UPDATE fundamentals
   SET pe_ratio           = NULL,
       pb_ratio           = NULL,
       ps_ratio           = NULL,
       ev_ebitda          = NULL,
       ev_to_revenue      = NULL,
       roe                = NULL,
       roa                = NULL,
       roic               = NULL,
       roce               = NULL,
       gross_margin       = NULL,
       operating_margin   = NULL,
       net_margin         = NULL,
       debt_equity        = NULL,
       interest_coverage  = NULL,
       current_ratio      = NULL,
       revenue_growth_yoy = NULL,
       eps_growth_yoy     = NULL,
       fcf_yield          = NULL,
       earnings_surprise  = NULL,
       dividend_yield     = NULL,
       market_cap         = NULL,
       enterprise_value   = NULL
 WHERE source IN ('yfinance', 'fmp')   -- pre-fix writers
    OR source IS NULL;

-- Tag the freshly-nulled rows so subsequent audits can distinguish
-- "cleaned by 018" from "written by the fixed writer" (yfinance_pit_v2).
-- Do NOT overwrite rows already tagged yfinance_pit_v2 (they were written
-- by the corrected code path and are honest per-period statements).
UPDATE fundamentals
   SET source = 'yfinance_cleaned_018'
 WHERE source IN ('yfinance', 'fmp')
    OR source IS NULL;

COMMIT;
