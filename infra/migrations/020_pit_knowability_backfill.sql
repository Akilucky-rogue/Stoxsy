-- Migration 020: backfill factor_pit.valid_from with knowability semantics.
--
-- Background (AUDIT_v2 C4):
--   pit_writer.write_factors_to_pit used to set `valid_from = datetime.now()`
--   (compute wall-clock). For any historical backfill, a 2015-dated factor
--   got valid_from = 2026-07-01, so the canonical bitemporal predicate
--     valid_from <= :as_of_date AND valid_to > :as_of_date
--   returned ZERO rows for all of history. pit_query() was decorative;
--   dataset.py / signal_generator.py bypassed the predicate and read
--   `valid_to = 'infinity'` (current snapshot) instead.
--
-- Fix:
--   pit_writer now stamps valid_from = as_of_date + 18h IST (see
--   qsde/factors/pit_writer.py::_knowability_timestamp). NSE closes at
--   15:30 IST; +18h gives a 2.5h publication buffer for bhavcopy / FII+DII
--   / macro releases. Rows written after this migration will already carry
--   the correct semantics.
--
--   This migration retroactively rewrites valid_from on existing rows so
--   the bitemporal predicate ALSO works for the millions of already-
--   backfilled historical rows.
--
-- Backfill rule:
--   For every row where valid_from is clearly a wall-clock timestamp
--   (defined as `valid_from > as_of_date + 2 days` — a real knowability
--   stamp would never be that far after the session it describes):
--     SET valid_from = as_of_date::timestamptz + INTERVAL '18 hours'
--     ...adjusted for the Asia/Kolkata timezone.
--
--   The guard leaves rows that were ALREADY written with knowability
--   semantics alone (idempotent — re-running is a no-op).
--
-- Duration warning:
--   `factor_pit` is a hypertable that may hold tens of millions of rows
--   in production. On a fresh dev DB this migration completes in <1s;
--   on a full 20y × 1500 symbol backfill (~90M rows) budget 5-10 minutes
--   with WAL noise. Not resumable — run during a maintenance window.

BEGIN;

-- The compute wall-clock stamp was tz-naive in the old writer; here we
-- interpret the target instant as 18:00 IST (Asia/Kolkata → +05:30). In
-- UTC that's 12:30. Postgres `AT TIME ZONE` works forward from a naive
-- timestamp to a timestamptz.
--
-- Formula:
--   new_valid_from = (as_of_date + INTERVAL '18 hours') AT TIME ZONE 'Asia/Kolkata'
--
-- The AT TIME ZONE interprets the naive `as_of_date + 18h` as a wall-clock
-- reading in Asia/Kolkata and returns the corresponding timestamptz.

UPDATE factor_pit
   SET valid_from = ((as_of_date::timestamp + INTERVAL '18 hours')
                    AT TIME ZONE 'Asia/Kolkata')
 WHERE valid_from > (as_of_date::timestamp + INTERVAL '2 days');

COMMIT;
