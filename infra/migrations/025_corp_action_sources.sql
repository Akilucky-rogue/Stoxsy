-- Migration 025: corporate-action events can now come from the NSE feed (source of record,
-- price-confirmed) as well as from the price-gap detector.
--   source            'nse_corp_actions' (parsed from the old StockTrack fetch) | 'price_inferred'
--   source_purpose    the NSE purpose text the factor was parsed from
--   ex_date_shift     sessions between the listed ex-date and the session whose gap confirmed it
--   kind SOURCE_UNCONFIRMED   the source lists a split/bonus but prices do not show it within +-2
--                             sessions: logged, NOT applied
-- Finding that motivated this: 458 of the first detector's 1,195 applied events sat at exactly 0.8
-- (an NSE 20% lower circuit, not a 1:4 bonus) and only 3% matched any NSE action.
BEGIN;

ALTER TABLE corp_action_events DROP CONSTRAINT IF EXISTS corp_action_events_kind_check;
ALTER TABLE corp_action_events ADD CONSTRAINT corp_action_events_kind_check
    CHECK (kind IN ('SPLIT_BONUS','CONSOLIDATION','UNEXPLAINED','SOURCE_UNCONFIRMED'));

ALTER TABLE corp_action_events ADD COLUMN IF NOT EXISTS source         TEXT NOT NULL DEFAULT 'price_inferred';
ALTER TABLE corp_action_events ADD COLUMN IF NOT EXISTS source_purpose TEXT;
ALTER TABLE corp_action_events ADD COLUMN IF NOT EXISTS ex_date_shift  INTEGER;

-- the NOT NULL measurements do not exist for a source row we could not locate in prices
ALTER TABLE corp_action_events ALTER COLUMN close_prev_session DROP NOT NULL;
ALTER TABLE corp_action_events ALTER COLUMN open_ex_date       DROP NOT NULL;
ALTER TABLE corp_action_events ALTER COLUMN close_ex_date      DROP NOT NULL;
ALTER TABLE corp_action_events ALTER COLUMN open_ratio         DROP NOT NULL;
ALTER TABLE corp_action_events ALTER COLUMN close_ratio        DROP NOT NULL;

COMMIT;
