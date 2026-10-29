"""Tests for the Scorecard skeleton (Phase 0): metrics + fidelity + tracker."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.scorecard import fidelity, metrics, tracker


def test_expectancy_and_hit_rate():
    rets = [0.02, -0.01, 0.03, -0.015, 0.01]
    assert metrics.hit_rate(rets) == pytest.approx(0.6)
    assert metrics.expectancy(rets) == pytest.approx(sum(rets) / 5)


def test_payoff_ratio():
    wl = metrics.win_loss_payoff([0.04, -0.02, 0.02])
    assert wl["n_win"] == 2 and wl["n_loss"] == 1
    assert wl["payoff_ratio"] == pytest.approx(0.03 / 0.02)


def test_brier_calibration():
    assert metrics.brier_score([1.0, 0.0], [1, 0]) == pytest.approx(0.0)   # perfect
    assert metrics.brier_score([0.5, 0.5], [1, 0]) == pytest.approx(0.25)  # coin flip


def test_fidelity_perfect_then_off():
    idx = pd.date_range("2026-06-02 09:15", periods=5, freq="min")
    ours = pd.DataFrame({"close": [100, 101, 102, 103, 104]}, index=idx)
    assert fidelity.reconcile_ohlc(ours, ours.copy())["overall_match_pct"] == 1.0
    off = ours.copy(); off.loc[idx[2], "close"] = 200.0
    assert fidelity.reconcile_ohlc(ours, off)["overall_match_pct"] < 1.0


def test_tracker_report_groups_by_engine():
    recs = [
        {"engine": "a", "realized_ret_net": 0.02, "confidence": 0.7, "hit": 1},
        {"engine": "a", "realized_ret_net": -0.01, "confidence": 0.6, "hit": 0},
        {"engine": "b", "realized_ret_net": 0.03},
    ]
    rep = tracker.report(recs)
    assert rep["by_engine"]["a"]["n"] == 2
    assert "brier" in rep["by_engine"]["a"]      # confidences present -> calibration computed
    assert rep["by_engine"]["b"]["n"] == 1
    assert rep["overall"]["n"] == 3
