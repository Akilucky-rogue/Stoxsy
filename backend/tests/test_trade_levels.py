"""
Direct tests for compute_trade_levels — the entry/stop/target/R:R money math.

Primary purpose: pin the CORR-1 regression. `compute_trade_levels` takes ATR as
a PERCENT (3.89 == 3.89%) and divides by 100 UNCONDITIONALLY. The old code used a
">1.0 means percent" auto-detect that silently misread low-volatility names whose
ATR% is below 1.0 (e.g. 0.97 -> treated as a 97% fraction), producing a NEGATIVE
stop and a ~+240% target. These tests fail loudly if that heuristic ever returns.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.risk.trade_levels import compute_trade_levels


def _lv(atr_pct, direction=1, horizon="swing", price=100.0, pred=None):
    return compute_trade_levels(price=price, atr_pct=atr_pct, predicted_return=pred,
                                direction=direction, horizon=horizon)


def test_low_vol_sub_one_percent_atr_is_sane():
    # 0.97% ATR — the CORR-1 case. Must be a small, sane stop/target.
    # Pre-fix this gave stop ~ -45 and target ~ +342.
    lv = _lv(0.97, direction=1, horizon="swing")
    assert lv["stop"] is not None and lv["target"] is not None
    assert 97.0 < lv["stop"] < 100.0 < lv["target"] < 105.0
    assert lv["risk_reward"] and lv["risk_reward"] > 0
    # The surfaced ATR is a fraction just under 1% — NOT ~0.97 (97%).
    assert lv["atr_pct"] == pytest.approx(0.0097)


def test_boundary_one_percent_no_flip():
    # 0.99 / 1.00 / 1.01 percent must be continuous (no >1.0 discontinuity).
    below, at, above = _lv(0.99)["stop"], _lv(1.0)["stop"], _lv(1.01)["stop"]
    assert below > at > above                       # bigger ATR -> lower long stop
    assert all(0 < s < 100 for s in (below, at, above))
    assert (below - at) < 0.05 and (at - above) < 0.05   # smooth, not a 100x jump


def test_percent_surfaced_as_fraction():
    assert _lv(2.0)["atr_pct"] == pytest.approx(0.02)   # 2.0% -> 0.02 fraction


def test_long_short_symmetry():
    lng, sht = _lv(3.0, direction=1), _lv(3.0, direction=-1)
    assert lng["stop"] < 100 < lng["target"]
    assert sht["target"] < 100 < sht["stop"]
    assert (lng["target"] - 100) == pytest.approx(100 - sht["target"])
    assert (100 - lng["stop"]) == pytest.approx(sht["stop"] - 100)


def test_hold_and_missing_atr():
    assert _lv(3.0, direction=0)["target"] is None       # HOLD -> no plan
    fb = _lv(None, direction=1, horizon="swing")          # missing ATR -> fallback
    assert fb["stop"] is not None and 0 < fb["stop"] < 100
    assert "atr_fallback" in (fb["notes"] or "")
