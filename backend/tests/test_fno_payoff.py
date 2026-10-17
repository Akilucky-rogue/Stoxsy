"""Tests for iron-condor settlement P&L (F&O increment 5) — the math that lets the
Scorecard measure the flagship's TRUE edge. All pure."""
from __future__ import annotations

import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.fno.payoff import (
    condor_params_from_legs,
    estimate_condor_cost,
    iron_condor_settlement_pnl,
)
from qsde.levels.options import build_iron_condor

# put_long 24300 < put_short 24500 < call_short 25500 < call_long 25700
# credit 95/unit, width 200, units 50 -> credit Rs4750, max loss Rs5250.
PARAMS = dict(put_long=24300.0, put_short=24500.0, call_short=25500.0, call_long=25700.0,
              net_credit_rupees=4750.0, units=50, max_loss_rupees=5250.0)


def _pnl(S, **kw):
    return iron_condor_settlement_pnl(settlement=S, **{**PARAMS, **kw})


def test_max_profit_between_short_strikes():
    r = _pnl(25000)
    assert r["pnl_gross"] == pytest.approx(4750.0)   # full credit kept
    assert r["status"] == "WIN"
    assert r["realized_ret"] == pytest.approx(4750 / 5250, abs=1e-4)


def test_max_loss_below_put_wing():
    r = _pnl(24000)                                   # below put_long
    assert r["pnl_gross"] == pytest.approx(-5250.0)   # capped at max loss
    assert r["status"] == "LOSS"
    assert r["realized_ret"] == pytest.approx(-1.0)


def test_max_loss_above_call_wing():
    assert _pnl(26000)["pnl_gross"] == pytest.approx(-5250.0)
    assert _pnl(26000)["realized_ret"] == pytest.approx(-1.0)


def test_partial_loss_in_put_ramp():
    r = _pnl(24400)                                   # between put_long & put_short
    # put liability = (24500-24400) = 100/unit; pnl_unit = 95-100 = -5 -> -250
    assert r["pnl_gross"] == pytest.approx(-250.0)
    assert r["status"] == "LOSS"


def test_breakeven_is_flat():
    r = _pnl(24500 - 95)                              # put_short - credit/unit
    assert r["pnl_gross"] == pytest.approx(0.0, abs=1e-6)


def test_cost_is_subtracted_for_net():
    cost = estimate_condor_cost(4750.0, cost_frac=0.10)
    assert cost == pytest.approx(475.0)
    r = _pnl(25000, cost_rupees=cost)
    assert r["pnl_net"] == pytest.approx(4750 - 475)
    assert r["realized_ret_net"] == pytest.approx((4750 - 475) / 5250, abs=1e-4)


def test_condor_params_from_legs_recovers_strikes():
    legs = [
        {"symbol": "a", "side": -1, "qty": 50, "strike": 24500, "opt_type": "PE"},
        {"symbol": "b", "side": +1, "qty": 50, "strike": 24300, "opt_type": "PE"},
        {"symbol": "c", "side": -1, "qty": 50, "strike": 25500, "opt_type": "CE"},
        {"symbol": "d", "side": +1, "qty": 50, "strike": 25700, "opt_type": "CE"},
    ]
    p = condor_params_from_legs(legs)
    assert p == {"put_short": 24500.0, "put_long": 24300.0,
                 "call_short": 25500.0, "call_long": 25700.0, "units": 50}


def test_condor_params_raises_on_incomplete_legs():
    with pytest.raises(ValueError):
        condor_params_from_legs([
            {"symbol": "a", "side": -1, "qty": 50, "strike": 24500, "opt_type": "PE"},
        ])


def test_builder_and_payoff_are_consistent():
    # The condor the engine builds must settle to its own declared max-profit /
    # max-loss — builder and payoff math agree by construction.
    idea = build_iron_condor(
        underlying="NIFTY", put_short=24500, put_long=24300,
        call_short=25500, call_long=25700,
        put_short_prem=120, put_long_prem=70, call_short_prem=110, call_long_prem=65,
        lots=1, lot_size=50, expiry=date(2026, 7, 31),
        symbols={"ps": "ps", "pl": "pl", "cs": "cs", "cl": "cl"},
    )
    legs = [asdict(leg) for leg in idea.legs]
    p = condor_params_from_legs(legs)
    win = iron_condor_settlement_pnl(net_credit_rupees=idea.net_credit, settlement=25000,
                                     max_loss_rupees=idea.max_loss_rupees, **p)
    loss = iron_condor_settlement_pnl(net_credit_rupees=idea.net_credit, settlement=23000,
                                      max_loss_rupees=idea.max_loss_rupees, **p)
    assert win["pnl_gross"] == pytest.approx(idea.max_profit)
    assert loss["pnl_gross"] == pytest.approx(-idea.max_loss_rupees)
