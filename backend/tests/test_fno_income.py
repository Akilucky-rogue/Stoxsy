"""Tests for the FnoIncomeEngine (F&O increment 2) — pure strike-selection brain.

Injected synthetic chain; deterministic strikes. Proves: emits a DEFINED-RISK
iron condor only when vol is rich, sizes the cap correctly, and skips when the
vol gate fails / a strike isn't quoted / the loss cap is exceeded.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.engines.base import Structure
from qsde.engines.fno_income import FnoIncomeEngine, expected_move

EXP = date(2026, 7, 31)


def _snap(iv_rank=0.7, chain=None):
    # spot 25000, atm_iv 15%, 7d -> 1sigma move ~519 -> short 24500P / 25500C, wings 200
    base = {(24500, "PE"): 120.0, (24300, "PE"): 70.0,
            (25500, "CE"): 110.0, (25700, "CE"): 65.0}
    return {"underlying": "NIFTY", "spot": 25000.0, "t_years": 7 / 365, "atm_iv": 0.15,
            "iv_rank": iv_rank, "expiry": EXP, "chain": base if chain is None else chain}


def _eng(snaps, **kw):
    return FnoIncomeEngine(lot_size=50, strike_step=50, source=lambda: snaps, **kw)


def test_expected_move():
    assert expected_move(25000, 0.15, 7 / 365) == pytest.approx(519.3, abs=1.0)


def test_emits_defined_risk_condor_when_vol_rich():
    ideas = _eng([_snap(0.7)]).scan()
    assert len(ideas) == 1
    idea = ideas[0]
    assert idea.structure == Structure.IRON_CONDOR and idea.direction == 0
    assert len(idea.legs) == 4
    # credit 95/unit, width 200 -> max loss 105/unit * lot 50 = 5250
    assert idea.max_loss_rupees == pytest.approx(105 * 50)
    assert idea.net_credit == pytest.approx(95 * 50)
    assert idea.confidence == pytest.approx(0.7)
    assert idea.breakevens == [pytest.approx(24405.0), pytest.approx(25595.0)]
    idea.validate()


def test_skips_when_iv_rank_below_min():
    assert _eng([_snap(0.3)]).scan() == []          # 0.3 < default gate 0.5


def test_skips_when_a_strike_is_not_quoted():
    chain = {(24500, "PE"): 120.0, (25500, "CE"): 110.0, (25700, "CE"): 65.0}  # missing 24300 PE wing
    assert _eng([_snap(0.7, chain=chain)]).scan() == []


def test_respects_max_loss_cap():
    assert _eng([_snap(0.7)], max_loss_cap=4000.0).scan() == []   # 5250 > 4000 -> dropped
