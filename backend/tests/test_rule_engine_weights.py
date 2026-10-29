"""Tests for the composite factor-weighting logic in qsde.research.rule_engine.

The backtest (scripts/backtest_rules.py) proved momentum (jt/mop) is the durable
edge and BAB/RSI2 are dead, and that a 60-session rolling live IC must NOT be
allowed to hijack the composite (June-2026 paper put ~100% weight on rsi2). The
shrinkage logic guarantees momentum keeps a hard floor no matter what live IC
says. These pure tests lock that invariant in.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.research.rule_engine import (
    MOMENTUM_PRIOR,
    PRIOR_SHRINK,
    shrink_toward_prior,
)


def _momentum_share(w: dict) -> float:
    total = sum(v for v in w.values() if v > 0)
    return (w.get("jt", 0.0) + w.get("mop", 0.0)) / total if total else 0.0


def test_cold_start_returns_prior():
    """No positive live IC -> the momentum prior, verbatim."""
    live = {"jt": 0.0, "mop": 0.0, "bab": 0.0, "rsi2": 0.0}
    assert shrink_toward_prior(live, MOMENTUM_PRIOR) == MOMENTUM_PRIOR


def test_rsi2_hijack_is_capped_by_the_floor():
    """The June-2026 failure mode: live IC says 100% rsi2. Momentum must still
    keep at least PRIOR_SHRINK of the weight."""
    live = {"jt": 0.0, "mop": 0.0, "bab": 0.0, "rsi2": 1.0}
    w = shrink_toward_prior(live, MOMENTUM_PRIOR, shrink=0.70)
    assert w["jt"] == pytest.approx(0.35)
    assert w["mop"] == pytest.approx(0.35)
    assert w["rsi2"] == pytest.approx(0.30)
    assert w["bab"] == pytest.approx(0.0)
    assert _momentum_share(w) == pytest.approx(0.70)


def test_momentum_floor_holds_for_any_live_distribution():
    """No live IC distribution can push momentum below the PRIOR_SHRINK floor."""
    for live in (
        {"jt": 0.0, "mop": 0.0, "bab": 1.0, "rsi2": 0.0},   # all BAB
        {"jt": 0.0, "mop": 0.0, "bab": 0.5, "rsi2": 0.5},   # split dead factors
        {"jt": 0.1, "mop": 0.0, "bab": 0.4, "rsi2": 0.5},   # a little jt
    ):
        w = shrink_toward_prior(live, MOMENTUM_PRIOR, shrink=PRIOR_SHRINK)
        assert _momentum_share(w) >= PRIOR_SHRINK - 1e-9
        assert sum(w.values()) == pytest.approx(1.0)  # convex combination


def test_live_momentum_ic_reinforces_not_dilutes():
    """When live IC ALSO favors momentum, momentum share exceeds the floor."""
    live = {"jt": 0.6, "mop": 0.4, "bab": 0.0, "rsi2": 0.0}
    w = shrink_toward_prior(live, MOMENTUM_PRIOR, shrink=0.70)
    assert _momentum_share(w) == pytest.approx(1.0)  # both prior and live all-momentum


def test_weights_sum_to_one():
    live = {"jt": 0.2, "mop": 0.1, "bab": 0.3, "rsi2": 0.4}
    w = shrink_toward_prior(live, MOMENTUM_PRIOR)
    assert sum(w.values()) == pytest.approx(1.0)
