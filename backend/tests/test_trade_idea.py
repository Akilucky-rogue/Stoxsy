"""
Contract tests for the TradeIdea schema (Phase 0 cornerstone).

The single most important guarantee: NO UNCAPPED RISK can pass validation, and
naked short options are banned outright — the Risk Engine relies on this.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.engines.base import Horizon, Leg, Structure, TradeIdea


def _cash(**kw) -> TradeIdea:
    base = dict(
        engine="t", symbol="RELIANCE", structure=Structure.CASH_LONG,
        horizon=Horizon.SWING, direction=1, entry=100.0, stop=98.0,
        target=104.0, max_loss_rupees=2000.0,
    )
    base.update(kw)
    return TradeIdea(**base)


def test_valid_cash_idea_passes():
    _cash().validate()  # must not raise


def test_uncapped_risk_is_rejected():
    for bad in (None, float("inf"), 0, -100):
        with pytest.raises(ValueError):
            _cash(max_loss_rupees=bad).validate()


def test_naked_short_option_is_banned():
    idea = TradeIdea(
        engine="t", symbol="NIFTY", structure=Structure.OPT_SHORT,
        horizon=Horizon.EXPIRY, direction=-1, max_loss_rupees=5000.0,
        legs=[Leg(symbol="NIFTY24JUL25000CE", side=-1, qty=50, opt_type="CE")],
    )
    with pytest.raises(ValueError):
        idea.validate()


def test_cash_idea_needs_stop():
    with pytest.raises(ValueError):
        _cash(stop=None).validate()


def test_option_structure_needs_legs():
    with pytest.raises(ValueError):
        TradeIdea(
            engine="t", symbol="NIFTY", structure=Structure.IRON_CONDOR,
            horizon=Horizon.EXPIRY, direction=0, max_loss_rupees=6000.0,
        ).validate()


def test_defined_risk_iron_condor_ok_and_actionable():
    legs = [
        Leg("NIFTY24JUL25000CE", -1, 50, strike=25000, opt_type="CE"),
        Leg("NIFTY24JUL25200CE", +1, 50, strike=25200, opt_type="CE"),
        Leg("NIFTY24JUL24000PE", -1, 50, strike=24000, opt_type="PE"),
        Leg("NIFTY24JUL23800PE", +1, 50, strike=23800, opt_type="PE"),
    ]
    idea = TradeIdea(
        engine="fno_income", symbol="NIFTY", structure=Structure.IRON_CONDOR,
        horizon=Horizon.EXPIRY, direction=0, legs=legs, net_credit=80.0,
        max_loss_rupees=6000.0, margin_required=120000.0,
    )
    idea.validate()                      # neutral + defined-risk -> ok
    assert idea.is_actionable            # condor is actionable despite direction 0
    assert idea.to_dict()["structure"] == "IRON_CONDOR"


def test_confidence_bounds():
    with pytest.raises(ValueError):
        _cash(confidence=1.4).validate()
    _cash(confidence=0.6).validate()     # in-range ok
