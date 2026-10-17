"""Tests for the defined-risk option-structure builders (Phase 0, levels/)."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.engines.base import Structure
from qsde.levels.options import build_credit_vertical, build_iron_condor

EXP = date(2026, 7, 31)


def test_bull_put_spread_defined_risk():
    # width 200, credit (120-70)=50 -> max loss/unit 150; units = 1*50
    idea = build_credit_vertical(
        underlying="NIFTY", opt_type="PE",
        short_strike=24000, long_strike=23800,
        short_premium=120, long_premium=70,
        lots=1, lot_size=50, expiry=EXP,
        short_symbol="NIFTY24JUL24000PE", long_symbol="NIFTY24JUL23800PE",
    )
    assert idea.structure == Structure.VERTICAL_SPREAD
    assert idea.direction == 1
    assert idea.max_loss_rupees == pytest.approx(150 * 50)
    assert idea.max_profit == pytest.approx(50 * 50)
    assert idea.breakevens[0] == pytest.approx(24000 - 50)
    idea.validate()                      # capped risk -> passes


def test_bear_call_spread_direction():
    idea = build_credit_vertical(
        underlying="NIFTY", opt_type="CE",
        short_strike=24500, long_strike=24700,
        short_premium=110, long_premium=60,
        lots=1, lot_size=50, expiry=EXP,
        short_symbol="c1", long_symbol="c2",
    )
    assert idea.direction == -1
    assert idea.breakevens[0] == pytest.approx(24500 + 50)


def test_credit_must_be_positive():
    with pytest.raises(ValueError):
        build_credit_vertical(
            underlying="NIFTY", opt_type="PE",
            short_strike=24000, long_strike=23800, short_premium=50, long_premium=70,
            lots=1, lot_size=50, expiry=EXP, short_symbol="a", long_symbol="b",
        )


def test_iron_condor_defined_risk_neutral():
    # put_credit 50, call_credit 45, total 95; widths 200/200; max loss/unit 105
    idea = build_iron_condor(
        underlying="NIFTY",
        put_short=24000, put_long=23800, call_short=24500, call_long=24700,
        put_short_prem=120, put_long_prem=70, call_short_prem=110, call_long_prem=65,
        lots=1, lot_size=50, expiry=EXP,
        symbols={"ps": "p1", "pl": "p2", "cs": "c1", "cl": "c2"},
    )
    assert idea.structure == Structure.IRON_CONDOR
    assert idea.direction == 0
    assert idea.is_actionable
    assert idea.max_loss_rupees == pytest.approx(105 * 50)
    assert idea.max_profit == pytest.approx(95 * 50)
    assert len(idea.breakevens) == 2
    idea.validate()


def test_iron_condor_bad_strike_order_rejected():
    with pytest.raises(ValueError):
        build_iron_condor(
            underlying="NIFTY",
            put_short=24600, put_long=23800, call_short=24500, call_long=24700,  # ps > cs
            put_short_prem=120, put_long_prem=70, call_short_prem=110, call_long_prem=65,
            lots=1, lot_size=50, expiry=EXP,
            symbols={"ps": "p1", "pl": "p2", "cs": "c1", "cl": "c2"},
        )
