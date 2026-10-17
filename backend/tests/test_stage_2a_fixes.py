"""Regression tests for Stage 2A of Session 2 (AUDIT_v2 correctness fixes).

Guards the fixes for:
  * H1 — ATR percent/fraction unit boundary (already fixed in
    qsde/risk/trade_levels.py; this test just makes sure it stays fixed).
  * M7 — SMC `_last_value` pivot-shift bug (the confirmation bar's high was
    being captured instead of the pivot bar's high, so BOS/CHoCH levels
    were offset by `right` bars).
  * H7 — Finnhub sentiment writer's ON CONFLICT clause (must include
    "source" after migration 012 broadened the PK).
  * H5 — Migration 017 must ship an IMMUTABLE-predicate active-factor
    index (init.sql's `WHERE valid_to > NOW()` fails silently on fresh
    installs; the replacement uses `valid_to = 'infinity'::timestamptz`).

Hermetic — no DB, no network. Uses hand-constructed OHLCV so hits are
verifiable by inspection.
"""
from __future__ import annotations

import inspect

import numpy as np
import pandas as pd
import pytest

from qsde.factors.smc import _last_value, bos_choch
from qsde.risk.trade_levels import compute_trade_levels


# ─────────────────────────────────────────────────────────────────────
# H1 — ATR unit boundary regression
# ─────────────────────────────────────────────────────────────────────

class TestATRUnitBoundary:
    """AUDIT_v2 H1: a calm mega-cap with ATR% < 1.0 must not blow up
    the trade plan. Prior heuristic ("if atr_pct > 1.0 divide by 100")
    treated 0.85 as an 85% fraction, giving a -64% stop and +240% target.

    Fix: unconditional divide-by-100 in compute_trade_levels (see the
    module docstring at qsde/risk/trade_levels.py:99-104).
    """

    def test_low_atr_produces_sensible_swing_stop(self):
        # A calm mega-cap: ₹2500 price, 0.85% ATR — well under the
        # old ">1.0 = percent" threshold.
        levels = compute_trade_levels(
            price=2500.0,
            atr_pct=0.85,               # 0.85%
            predicted_return=0.02,      # 2%
            direction=+1,
            horizon="swing",
        )
        # Swing stop mult = 1.5 * ATR (per _HORIZON_MULTIPLIERS).
        # ATR fraction = 0.0085, ATR_abs = 21.25, stop_distance = 31.875.
        # Buy → stop = 2500 - 31.875 = 2468.125.
        assert levels["stop"] is not None
        assert levels["stop"] == pytest.approx(2468.13, abs=0.05)
        # Stop must be BELOW entry for a buy (was NEGATIVE under the old
        # heuristic).
        assert levels["stop"] < levels["entry"]
        # Stop distance must be a couple of percent, not 60%+.
        stop_pct = (levels["entry"] - levels["stop"]) / levels["entry"]
        assert 0.005 < stop_pct < 0.05

    def test_low_atr_produces_sensible_intraday_stop(self):
        # Same low-ATR calm large-cap on intraday.
        levels = compute_trade_levels(
            price=1000.0,
            atr_pct=0.5,                # 0.5% intraday ATR
            predicted_return=0.01,
            direction=+1,
            horizon="intraday",
        )
        # intraday stop_mult = 0.75, ATR fraction = 0.005,
        # stop_distance = 0.75 * 0.005 * 1000 = 3.75.
        assert levels["stop"] == pytest.approx(996.25, abs=0.05)
        assert levels["stop"] < levels["entry"]

    def test_boundary_atr_1pct_still_sensible(self):
        # Exactly the old threshold. Must be treated as percent, not as
        # a fraction meaning 100%.
        levels = compute_trade_levels(
            price=500.0,
            atr_pct=1.0,                # exactly 1.0%
            predicted_return=0.02,
            direction=+1,
            horizon="swing",
        )
        # ATR fraction = 0.01, ATR_abs = 5, stop_distance = 7.5.
        # Buy → stop = 492.5.
        assert levels["stop"] == pytest.approx(492.5, abs=0.05)


# ─────────────────────────────────────────────────────────────────────
# M7 — SMC pivot-shift in _last_value
# ─────────────────────────────────────────────────────────────────────

class TestSMCPivotShift:
    """AUDIT_v2 M7: `_last_value(high, sh)` used to return the confirmation
    bar's high (index t) at positions where sh==1, but sh is already shifted
    by `right`, so we actually want the pivot bar's high (index t-right).
    """

    def test_last_value_signature_accepts_pivot_offset(self):
        """Guard that the fix signature exists so future edits don't quietly
        drop the parameter."""
        sig = inspect.signature(_last_value)
        assert "pivot_offset" in sig.parameters, (
            "M7 fix requires _last_value(pivot_offset=...) — do not remove"
        )

    def test_last_value_captures_pivot_bar_not_confirmation_bar(self):
        # Construct a series where the pivot bar's value is distinct from
        # any later bar. Value at index 2 = 100 (the pivot). Mask (already
        # shifted by right=2) fires at index 4. With the fix, _last_value
        # should return the value from index 2, not index 4.
        series = pd.Series([np.nan, np.nan, 100.0, 50.0, 25.0])
        mask   = pd.Series([0,      0,      0,     0,    1  ])
        out = _last_value(series, mask, pivot_offset=2)
        assert out.iloc[4] == 100.0, (
            "With pivot_offset=2 and mask firing at index 4, _last_value "
            "must return series[4-2]=100, not series[4]=25"
        )

    def test_last_value_default_offset_is_backward_compatible(self):
        # Callers that pass no offset (there aren't any today, but future
        # code shouldn't be surprised) get identical behavior to the old
        # `series.where(mask).ffill()`.
        series = pd.Series([10.0, 20.0, 30.0, 40.0])
        mask   = pd.Series([1,    0,    1,    0])
        out = _last_value(series, mask)                 # no pivot_offset
        expected = pd.Series([10.0, 10.0, 30.0, 30.0])
        pd.testing.assert_series_equal(out, expected, check_names=False)

    def test_bos_up_fires_only_after_pivot_high_broken(self):
        """End-to-end: BOS_up must trigger when close breaks the actual
        pivot bar's high, not the confirmation bar's high.
        """
        n = 20
        close = pd.Series(
            [95, 96, 98, 100, 105, 110, 108, 105, 102, 106,
             111, 108, 109, 112, 108, 106, 104, 108, 110, 112],
            dtype=float,
        )
        high = close + 1
        low = close - 1
        df = pd.DataFrame({
            "open": close, "high": high, "low": low, "close": close,
            "volume": 1000,
        })
        out = bos_choch(df, left=2, right=2)
        bos_up = out["smc_bos_up"]
        first_fire = bos_up[bos_up == 1].index.min() if (bos_up == 1).any() else None
        # Under the M7 bug, BOS_up could fire against the wrong (confirmation-
        # bar) high, producing spurious early fires. With the fix, the first
        # fire must be at or after the pivot's real high is broken by a close.
        if first_fire is not None:
            assert first_fire >= 10, (
                f"BOS_up fired at index {first_fire}, before the pivot's "
                f"real high is broken. Sign of the M7 bug."
            )


# ─────────────────────────────────────────────────────────────────────
# H7 — Finnhub ON CONFLICT includes "source"
# ─────────────────────────────────────────────────────────────────────

class TestFinnhubOnConflict:
    """AUDIT_v2 H7: migration 012 changed news_sentiment PK to
    (symbol, date, source) — Postgres rejects ON CONFLICT (symbol, date)
    when that's not a unique constraint. Guard by reading the source.
    """

    def test_sync_sentiment_uses_source_in_conflict_columns(self):
        from qsde.ingestion import finnhub_client
        src = inspect.getsource(finnhub_client.sync_sentiment_to_db)
        assert 'conflict_columns=["symbol", "date", "source"]' in src, (
            "H7 fix requires conflict_columns to include 'source' — "
            "migration 012 broadened the PK and Postgres will reject the "
            "old (symbol, date)-only variant"
        )


# ─────────────────────────────────────────────────────────────────────
# Migration 017 exists and has correct predicate
# ─────────────────────────────────────────────────────────────────────

class TestMigration017:
    """AUDIT_v2 H5: init.sql's idx_factor_pit_lookup has WHERE valid_to > NOW()
    which is not IMMUTABLE. Migration 017 must ship the corrected predicate.
    """

    def test_migration_017_installs_immutable_predicate(self):
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[2]
        mig = repo_root / "infra" / "migrations" / "017_active_factor_pit_index.sql"
        assert mig.exists(), f"Migration 017 must exist at {mig}"
        content = mig.read_text()
        # Strip SQL comment lines so we validate EXECUTABLE SQL only
        # (the file's comment block references the broken predicate to
        # explain why we replaced it — that reference is fine).
        exec_sql = "\n".join(
            line for line in content.splitlines()
            if not line.lstrip().startswith("--")
        )
        assert "'infinity'::timestamptz" in exec_sql, (
            "Migration 017 must use the IMMUTABLE literal predicate "
            "`valid_to = 'infinity'::timestamptz`."
        )
        assert "DROP INDEX IF EXISTS idx_factor_pit_lookup" in exec_sql
        assert "CREATE INDEX IF NOT EXISTS idx_factor_pit_lookup" in exec_sql
        # The specific broken pattern must NOT appear in the executable SQL.
        assert "valid_to > NOW()" not in exec_sql, (
            "Migration 017 must NOT reintroduce the broken "
            "`WHERE valid_to > NOW()` predicate — that is exactly the "
            "H5 bug this migration fixes."
        )
