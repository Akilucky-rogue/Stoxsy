"""Tests for the first migrated engine (Phase 0 step 5): RuleMomentumEngine.

Guarantees under test: every emitted idea is a valid, DEFINED-RISK TradeIdea,
sizing goes through the ONE shared sizer (so the budget/affordability constraint
actually binds), and junk rows (flat / missing stop) are skipped — all proven
without a DB via an injected signal source.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.engines.base import Structure
from qsde.engines.rule_momentum import (
    DEFAULT_ACTIONABLE_HORIZONS,
    DEFAULT_ACTIONABLE_STRATEGIES,
    RuleMomentumEngine,
)


def _eng(rows, risk=2000.0, budget=1_000_000.0) -> RuleMomentumEngine:
    return RuleMomentumEngine(risk_rupees=risk, budget=budget, source=lambda: rows)


def test_actionable_defaults_are_the_proven_momentum_signal():
    """The engine's ACTIONABLE output defaults to the proven, long-only momentum
    composite on swing (scripts/backtest_rules.py) — NOT the dead BAB/RSI2 factors
    or the diluted equal-weight composite."""
    eng = RuleMomentumEngine()
    assert eng.actionable_strategies == DEFAULT_ACTIONABLE_STRATEGIES == ("tier1_mom",)
    assert eng.actionable_horizons == DEFAULT_ACTIONABLE_HORIZONS == ("swing",)
    assert eng.long_only is True


def test_scan_emits_sized_defined_risk_ideas():
    rows = [
        {"strategy": "tier1_jt", "symbol": "RELIANCE", "horizon": "rotation", "direction": 1,
         "entry_price": 100.0, "stop_price": 98.0, "target_price": 105.0, "is_liquid": True},
        {"strategy": "tier1_mop", "symbol": "TCS", "horizon": "long", "direction": -1,
         "entry_price": 200.0, "stop_price": 204.0, "target_price": 190.0, "is_liquid": True},
    ]
    ideas = _eng(rows).scan()
    assert len(ideas) == 2
    long_idea = ideas[0]
    assert long_idea.structure == Structure.CASH_LONG and long_idea.direction == 1
    # risk/unit 2.0, risk budget 2000, ample capital -> qty 1000, max_loss 2000.
    assert long_idea.qty == 1000
    assert long_idea.max_loss_rupees == pytest.approx(2000.0)
    assert long_idea.capital_required == pytest.approx(100000.0)
    long_idea.validate()
    assert ideas[1].structure == Structure.CASH_SHORT and ideas[1].direction == -1


def test_budget_constraint_now_actually_binds():
    # The whole point of the fix: a ₹500 stock with only ₹10k budget can afford
    # 20 shares; the per-trade risk cap would allow far more, but affordability
    # now BINDS (the old inline sizing ignored budget entirely).
    rows = [{"strategy": "t", "symbol": "PRICEY", "horizon": "swing", "direction": 1,
             "entry_price": 500.0, "stop_price": 490.0, "target_price": 540.0, "is_liquid": True}]
    idea = _eng(rows, risk=5000.0, budget=10_000.0).scan()[0]
    assert idea.qty == 20                                  # floor(10000 / 500)
    assert idea.capital_required == pytest.approx(10_000.0)
    assert idea.max_loss_rupees == pytest.approx(20 * 10)  # 200, NOT the 5000 risk cap
    idea.validate()


def test_scan_skips_flat_and_missing_stop():
    rows = [
        {"strategy": "t", "symbol": "A", "horizon": "swing", "direction": 0,
         "entry_price": 100, "stop_price": 98, "is_liquid": True},          # flat
        {"strategy": "t", "symbol": "B", "horizon": "swing", "direction": 1,
         "entry_price": 100, "stop_price": None, "is_liquid": True},        # no stop
        {"strategy": "t", "symbol": "C", "horizon": "swing", "direction": 1,
         "entry_price": 100, "stop_price": 100, "is_liquid": True},         # zero risk
    ]
    assert _eng(rows).scan() == []


def test_every_emitted_idea_is_defined_risk():
    rows = [{"strategy": "tier1_bab", "symbol": "X", "horizon": "swing", "direction": 1,
             "entry_price": 500, "stop_price": 490, "target_price": 520, "is_liquid": True}]
    ideas = _eng(rows).scan()
    assert ideas
    for idea in ideas:
        idea.validate()                       # must not raise
        assert idea.max_loss_rupees and idea.max_loss_rupees > 0
        assert idea.qty and idea.qty > 0
        assert idea.engine.startswith("rule_momentum:")
