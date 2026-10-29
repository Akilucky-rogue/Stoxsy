-- Migration 017: fix the broken `idx_factor_pit_lookup` predicate.
--
-- Background (AUDIT_v2 H5):
--   init.sql ships `CREATE INDEX ... ON factor_pit(...) WHERE valid_to > NOW()`.
--   PostgreSQL requires index predicates to be IMMUTABLE. NOW() is STABLE,
--   so this statement fails on every fresh install — silently, because
--   psycopg runs init.sql statement-by-statement and the error only lands
--   in the log. The rest of init.sql still applies, but the serve path
--   ends up with NO active-factor index at 87M+ rows and the DISTINCT ON
--   query in signal_generator degrades to a seq scan.
--
--   The working replacement — `WHERE valid_to = 'infinity'` — lived in
--   the archived script `backend/scripts/_archive/create_active_index.py`.
--   This migration installs the correct index and drops the broken one
--   if it happens to exist on a machine where it succeeded (older
--   PostgreSQL versions were less strict, and TimescaleDB partial indexes
--   sometimes slip through).
--
-- Correctness note: `valid_to = 'infinity'::timestamptz` selects exactly
-- the "current active" version of each (symbol, as_of_date, factor_name)
-- row under the PIT writer's design (writer sets prior version's valid_to
-- to now() on update, new row's valid_to to 'infinity'). Idempotent —
-- IF EXISTS + IF NOT EXISTS guards allow re-running safely.

BEGIN;

-- Drop the broken index if it exists (from a stale schema or a permissive
-- Postgres version).
DROP INDEX IF EXISTS idx_factor_pit_lookup;

-- Install the corrected index. The predicate is now IMMUTABLE: comparing
-- against the literal 'infinity'::timestamptz doesn't call any function.
CREATE INDEX IF NOT EXISTS idx_factor_pit_lookup
    ON factor_pit(symbol, as_of_date, factor_name)
    WHERE valid_to = 'infinity'::timestamptz;

COMMIT;
