"""Tests for qsde/universe/liquidity_filter.py.

Hermetic — `compute_adv` is exercised via a monkey-patched `read_sql` that
returns synthetic OHLCV. `classify_tier` is pure and tested directly.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from qsde.universe import liquidity_filter as lf
from qsde.universe.liquidity_filter import (
    MARGINAL_MIN_RUPEES,
    TIER_ILLIQUID,
    TIER_MARGINAL,
    TIER_TRADEABLE,
    TRADEABLE_MIN_RUPEES,
    classify_tier,
    classify_universe,
    compute_adv,
)


# ──────────────────────────────────────────────────────────────────────
# classify_tier — pure function
# ──────────────────────────────────────────────────────────────────────

class TestClassifyTier:
    def test_above_tradeable_threshold_is_tradeable(self):
        # ₹15cr ADV, 60 sessions
        assert classify_tier(15 * 1e7, n_sessions=60) == TIER_TRADEABLE

    def test_at_exact_tradeable_boundary_is_tradeable(self):
        assert classify_tier(TRADEABLE_MIN_RUPEES, n_sessions=60) == TIER_TRADEABLE

    def test_between_marginal_and_tradeable_is_marginal(self):
        # ₹5cr
        assert classify_tier(5 * 1e7, n_sessions=60) == TIER_MARGINAL

    def test_at_marginal_boundary_is_marginal(self):
        assert classify_tier(MARGINAL_MIN_RUPEES, n_sessions=60) == TIER_MARGINAL

    def test_below_marginal_is_illiquid(self):
        assert classify_tier(0.5 * 1e7, n_sessions=60) == TIER_ILLIQUID

    def test_zero_adv_is_illiquid(self):
        assert classify_tier(0.0, n_sessions=60) == TIER_ILLIQUID

    def test_none_adv_is_illiquid(self):
        assert classify_tier(None, n_sessions=60) == TIER_ILLIQUID

    def test_insufficient_history_is_illiquid_regardless_of_adv(self):
        # Hot first-day listing with massive ADV but < 20 sessions — still illiquid.
        assert classify_tier(100 * 1e7, n_sessions=5) == TIER_ILLIQUID


# ──────────────────────────────────────────────────────────────────────
# compute_adv via monkey-patched read_sql
# ──────────────────────────────────────────────────────────────────────

def _synthetic_ohlcv(symbol: str, n_days: int, close: float = 100.0,
                     volume: float = 1_000_000.0,
                     start: str = "2026-01-01") -> pd.DataFrame:
    """One symbol, n_days of constant close+volume — easy hand-derivable ADV."""
    dates = pd.bdate_range(start=start, periods=n_days)
    return pd.DataFrame({
        "symbol": symbol,
        "date": dates,
        "close": close,
        "volume": volume,
    })


class TestComputeAdv:
    def test_single_symbol_constant_volume_computes_adv(self, monkeypatch):
        # 60 sessions × close=100 × volume=1M = ADV = 100M = ₹10cr exactly.
        df = _synthetic_ohlcv("TEST", n_days=60, close=100.0, volume=1_000_000.0)
        monkeypatch.setattr(lf, "read_sql", lambda *a, **kw: df)
        out = compute_adv(window_days=60, as_of_date=date(2026, 6, 1))
        assert len(out) == 1
        assert out.iloc[0]["symbol"] == "TEST"
        assert out.iloc[0]["adv_60d"] == pytest.approx(100 * 1_000_000)
        assert int(out.iloc[0]["n_sessions"]) == 60

    def test_empty_input_returns_empty(self, monkeypatch):
        monkeypatch.setattr(lf, "read_sql", lambda *a, **kw: pd.DataFrame())
        out = compute_adv()
        assert out.empty
        assert list(out.columns) == ["symbol", "adv_60d", "n_sessions", "latest_session"]


# ──────────────────────────────────────────────────────────────────────
# classify_universe — composition
# ──────────────────────────────────────────────────────────────────────

class TestClassifyUniverse:
    def test_assigns_tiers_across_multiple_symbols(self, monkeypatch):
        # Three symbols at known ADV levels (ADV = close × volume on a constant
        # synthetic series). Thresholds: tradeable >= ₹10cr, marginal >= ₹1cr.
        # HIGH:  close=1000, vol=2M    -> ADV = ₹200cr -> tradeable
        # MID:   close=100,  vol=200k  -> ADV = ₹2cr   -> marginal (in [₹1cr, ₹10cr))
        # LOW:   close=10,   vol=100   -> ADV = ₹1k    -> illiquid
        rows = []
        for sym, close, vol in (("HIGH", 1000, 2_000_000),
                                ("MID",  100,  200_000),
                                ("LOW",  10,   100)):
            rows.append(_synthetic_ohlcv(sym, n_days=60, close=close, volume=vol))
        full = pd.concat(rows, ignore_index=True)
        monkeypatch.setattr(lf, "read_sql", lambda *a, **kw: full)
        out = classify_universe(window_days=60)
        tiers = dict(zip(out["symbol"], out["tier"]))
        assert tiers["HIGH"] == TIER_TRADEABLE
        assert tiers["MID"] == TIER_MARGINAL    # ₹2cr -> marginal band
        assert tiers["LOW"] == TIER_ILLIQUID


# ──────────────────────────────────────────────────────────────────────
# Module invariants
# ──────────────────────────────────────────────────────────────────────

class TestInvariants:
    def test_tier_thresholds_ordered(self):
        assert TRADEABLE_MIN_RUPEES > MARGINAL_MIN_RUPEES
        assert MARGINAL_MIN_RUPEES > 0
