"""Regression tests for Stage 2B.2 of Session 2 (AUDIT_v2 C4 — PIT semantics).

C4 bug: pit_writer stamped `valid_from = datetime.now()` (compute wall-clock).
For any historical backfill, a 2015 row got valid_from = 2026-*, so the
bitemporal predicate `valid_from <= as_of_date AND valid_to > as_of_date`
returned ZERO rows for all of history. pit_query() had zero callers.

Fix: valid_from = as_of_date + 18h IST (see _knowability_timestamp). Now
the bitemporal predicate actually works for historical dates.

Hermetic — no DB, no network. Uses source inspection + direct calls to
_knowability_timestamp.
"""
from __future__ import annotations

import inspect
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest


# ─────────────────────────────────────────────────────────────────────
# _knowability_timestamp — the core of the C4 fix
# ─────────────────────────────────────────────────────────────────────

class TestKnowabilityTimestamp:
    def test_returns_18h_ist_for_a_session_date(self):
        from qsde.factors.pit_writer import _knowability_timestamp
        ts = _knowability_timestamp(date(2020, 6, 30))
        # Must be tz-aware in Asia/Kolkata.
        assert ts.tzinfo is not None
        offset_hours = ts.utcoffset().total_seconds() / 3600
        assert offset_hours == 5.5, (
            f"Knowability timestamp must be in IST (+5:30), got {offset_hours}h."
        )
        # Wall-clock: 18:00 on the session date.
        assert ts.year == 2020 and ts.month == 6 and ts.day == 30
        assert ts.hour == 18 and ts.minute == 0

    def test_accepts_pandas_timestamp_and_string_forms(self):
        from qsde.factors.pit_writer import _knowability_timestamp
        # str form
        ts_str = _knowability_timestamp("2015-01-15")
        assert ts_str.year == 2015 and ts_str.day == 15
        # pandas Timestamp
        ts_pd = _knowability_timestamp(pd.Timestamp("2015-01-15"))
        assert ts_pd.year == 2015 and ts_pd.day == 15

    def test_historical_backfill_gets_historical_valid_from(self):
        """The load-bearing property: a 2015-06-30 row must NOT get a
        2026 valid_from anymore."""
        from qsde.factors.pit_writer import _knowability_timestamp
        ts = _knowability_timestamp(date(2015, 6, 30))
        assert ts.year == 2015, (
            f"C4 regressed: a 2015 session's knowability timestamp is "
            f"stamped in {ts.year}. Historical backfill must stamp "
            f"historical valid_from, not wall-clock now."
        )


# ─────────────────────────────────────────────────────────────────────
# pit_writer.write_factors_to_pit — no more wall-clock valid_from
# ─────────────────────────────────────────────────────────────────────

class TestPitWriterNoWallClock:
    def test_writer_uses_knowability_not_now(self):
        """Source-level guard: the writer must not stamp all rows with a
        single `datetime.now()`. It must call _knowability_timestamp."""
        from qsde.factors import pit_writer
        src = inspect.getsource(pit_writer.write_factors_to_pit)
        # The old buggy code was:  now = datetime.now(); long_df["valid_from"] = now
        # That pattern must be gone.
        assert 'long_df["valid_from"] = now' not in src, (
            "C4 regressed: writer is stamping all rows with `now` again — "
            "must call _knowability_timestamp per-row instead."
        )
        assert "_knowability_timestamp" in src, (
            "Writer must derive valid_from via _knowability_timestamp so "
            "historical backfills stamp historical valid_from values."
        )

    def test_writer_docstring_calls_out_c4_fix(self):
        """The module docstring must document the C4 fix so future readers
        don't reintroduce datetime.now()."""
        from qsde.factors import pit_writer
        doc = (pit_writer.__doc__ or "")
        assert "C4" in doc, (
            "pit_writer.__doc__ must reference AUDIT_v2 C4 so the "
            "knowability semantics aren't accidentally regressed."
        )


# ─────────────────────────────────────────────────────────────────────
# pit_query — correct bitemporal predicate
# ─────────────────────────────────────────────────────────────────────

class TestPitQueryPredicate:
    def test_pit_query_uses_end_of_session_predicate(self):
        """pit_query must ask 'knowable BY THE END OF as_of_date' — i.e.
        compare against as_of_date + 23:59:59, not against as_of_date at
        midnight (which would REJECT rows stamped at 18:00 IST of that
        session)."""
        from qsde.db import connection
        src = inspect.getsource(connection.pit_query)
        # The predicate must include a 23h/59m/59s offset, or an equivalent
        # end-of-day form.
        assert "INTERVAL '23 hours 59 minutes 59 seconds'" in src, (
            "pit_query must compare valid_from against end-of-day of "
            "as_of_date (as_of_date + 23:59:59), not against midnight. "
            "Rows with valid_from = as_of_date + 18h would otherwise be "
            "rejected on their own session."
        )
        # And the cast must be timestamptz, not timestamp.
        assert "::timestamptz" in src

    def test_pit_query_bulk_exists_and_takes_symbol_list(self):
        """A bulk variant is needed for dataset.py / rule_engine to actually
        use PIT semantics in training reads. Stage 2C will wire it; Stage
        2B.2 just proves it's there and correctly shaped."""
        from qsde.db import connection
        assert hasattr(connection, "pit_query_bulk"), (
            "pit_query_bulk must exist for batched training reads."
        )
        sig = inspect.signature(connection.pit_query_bulk)
        # First arg must accept a list of symbols.
        params = list(sig.parameters.keys())
        assert params[0] == "symbols"
        assert params[1] == "as_of_date"


# ─────────────────────────────────────────────────────────────────────
# Migration 020 — backfill valid_from for existing rows
# ─────────────────────────────────────────────────────────────────────

class TestMigration020:
    def test_migration_020_exists_and_uses_ist_timezone(self):
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[2]
        mig = repo_root / "infra" / "migrations" / "020_pit_knowability_backfill.sql"
        assert mig.exists(), f"Migration 020 must exist at {mig}"
        sql = mig.read_text()
        exec_sql = "\n".join(
            line for line in sql.splitlines()
            if not line.lstrip().startswith("--")
        )
        # Must UPDATE factor_pit.
        assert "UPDATE factor_pit" in exec_sql
        # Must derive valid_from from as_of_date + 18 hours IST.
        assert "INTERVAL '18 hours'" in exec_sql, (
            "Migration 020 must set valid_from = as_of_date + 18h IST (the "
            "knowability offset) so historical rows get honest valid_from "
            "and pit_query works retroactively."
        )
        assert "AT TIME ZONE 'Asia/Kolkata'" in exec_sql, (
            "Migration 020 must interpret the 18h offset in IST via "
            "AT TIME ZONE 'Asia/Kolkata' — using UTC or a naive timestamp "
            "would shift every historical valid_from by 5.5h."
        )

    def test_migration_020_is_idempotent(self):
        """Re-running the migration must be a no-op. The WHERE clause must
        only touch rows whose valid_from is clearly wall-clock-tagged
        (far ahead of as_of_date)."""
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[2]
        mig = repo_root / "infra" / "migrations" / "020_pit_knowability_backfill.sql"
        sql = mig.read_text()
        exec_sql = "\n".join(
            line for line in sql.splitlines()
            if not line.lstrip().startswith("--")
        )
        # Idempotency guard: WHERE valid_from > as_of_date + N days.
        # After the first run, valid_from will be as_of_date + 18h,
        # NOT far ahead — so the second run's WHERE matches nothing.
        assert "valid_from >" in exec_sql, (
            "Migration 020 must include a WHERE clause that only picks "
            "wall-clock-tagged rows (idempotency guard)."
        )
        assert "INTERVAL '2 days'" in exec_sql or \
               "INTERVAL '1 day'" in exec_sql, (
            "Idempotency guard must use a 1+ day threshold — a knowability "
            "stamp is always <= session + 24h."
        )


# ─────────────────────────────────────────────────────────────────────
# End-to-end simulation via _knowability_timestamp + the SQL predicate
# ─────────────────────────────────────────────────────────────────────

class TestPitBitemporalSimulation:
    """Confirm the two-halves fit: a row stamped by _knowability_timestamp
    on 2020-06-30 must clear the pit_query predicate at as_of_date=2020-06-30
    but NOT at as_of_date=2020-06-29 (no lookahead)."""

    def test_knowable_row_clears_predicate_on_its_own_session(self):
        from qsde.factors.pit_writer import _knowability_timestamp
        ts = _knowability_timestamp(date(2020, 6, 30))
        # Predicate uses as_of_date + 23:59:59 IST (end of that session).
        end_of_session = datetime(2020, 6, 30, 23, 59, 59, tzinfo=ZoneInfo("Asia/Kolkata"))
        assert ts <= end_of_session, (
            f"Knowability stamp {ts} must be <= end-of-session "
            f"{end_of_session} for pit_query to return the row on its "
            f"own session."
        )

    def test_knowable_row_fails_predicate_on_prior_session_no_lookahead(self):
        from qsde.factors.pit_writer import _knowability_timestamp
        # Row is knowable on 2020-06-30. Query for 2020-06-29 must NOT
        # return it (that would be lookahead).
        ts = _knowability_timestamp(date(2020, 6, 30))
        end_of_prior_session = datetime(2020, 6, 29, 23, 59, 59, tzinfo=ZoneInfo("Asia/Kolkata"))
        assert ts > end_of_prior_session, (
            f"Knowability stamp {ts} must be > end-of-prior-session "
            f"{end_of_prior_session} — otherwise pit_query at 2020-06-29 "
            f"would leak a 2020-06-30-computed value into that day's "
            f"backtest read. THIS IS THE LOOKAHEAD GUARANTEE."
        )
