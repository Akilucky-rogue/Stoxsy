"""Tests for the F&O data bridge (increment 3): bhavcopy parse + IV analytics,
ending in the full pipeline option-data -> chain snapshot -> FnoIncomeEngine."""
from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.engines.fno_income import FnoIncomeEngine
from qsde.fno.black_scholes import bs_price
from qsde.fno.bhavcopy import parse_fo_bhavcopy
from qsde.fno.vol import add_iv, atm_iv, build_chain_snapshot, iv_rank

EXP = date(2026, 7, 31)
ASOF = date(2026, 7, 24)   # 7 days to expiry

LEGACY = """INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,CLOSE,SETTLE_PR,OPEN_INT
OPTIDX,NIFTY,31-Jul-2026,25000,CE,150.5,150.5,1000
OPTIDX,NIFTY,31-Jul-2026,25000,PE,140.2,140.2,1200
FUTIDX,NIFTY,31-Jul-2026,0,XX,25010,25010,500
"""

UDIFF = """FinInstrmTp,TckrSymb,XpryDt,StrkPric,OptnTp,SttlmPric,OpnIntrst
IDO,NIFTY,2026-07-31,25000,CE,150.5,1000
IDO,NIFTY,2026-07-31,25000,PE,140.2,1200
IDF,NIFTY,2026-07-31,0,,25010,500
"""


def test_parse_legacy_drops_futures_keeps_options():
    df = parse_fo_bhavcopy(LEGACY)
    assert len(df) == 2 and set(df["opt_type"]) == {"CE", "PE"}
    assert df.iloc[0]["expiry"] == EXP
    assert float(df[df["opt_type"] == "CE"].iloc[0]["settle"]) == 150.5


def test_parse_udiff_format():
    df = parse_fo_bhavcopy(UDIFF)
    assert len(df) == 2 and set(df["opt_type"]) == {"CE", "PE"}
    assert df.iloc[0]["expiry"] == EXP


def test_add_iv_recovers_known_vol():
    price = bs_price(25000, 25000, 7 / 365, 0.065, 0.18, "CE")
    df = pd.DataFrame([{"strike": 25000, "opt_type": "CE", "settle": price, "expiry": EXP}])
    out = add_iv(df, spot=25000, asof=ASOF)
    assert out.iloc[0]["iv"] == pytest.approx(0.18, abs=1e-3)


def test_atm_iv_uses_nearest_strike():
    df = pd.DataFrame([
        {"strike": 24900, "opt_type": "CE", "settle": 1, "expiry": EXP, "iv": 0.20},
        {"strike": 25000, "opt_type": "CE", "settle": 1, "expiry": EXP, "iv": 0.15},
        {"strike": 25000, "opt_type": "PE", "settle": 1, "expiry": EXP, "iv": 0.17},
    ])
    assert atm_iv(df, spot=25010) == pytest.approx((0.15 + 0.17) / 2)


def test_iv_rank():
    assert iv_rank(0.15, [0.10, 0.20]) == pytest.approx(0.5)
    assert iv_rank(0.20, [0.10, 0.20]) == pytest.approx(1.0)
    assert iv_rank(0.05, [0.10, 0.20]) == pytest.approx(0.0)   # clamped to [0,1]
    assert math.isnan(iv_rank(0.15, []))


def test_full_pipeline_bhavcopy_data_to_engine_idea():
    # option-data -> add_iv -> chain snapshot -> FnoIncomeEngine -> defined-risk condor
    ce_atm = bs_price(25000, 25000, 7 / 365, 0.065, 0.15, "CE")
    pe_atm = bs_price(25000, 25000, 7 / 365, 0.065, 0.15, "PE")
    rows = [
        {"strike": 25000, "opt_type": "CE", "settle": ce_atm, "expiry": EXP},
        {"strike": 25000, "opt_type": "PE", "settle": pe_atm, "expiry": EXP},
        {"strike": 24500, "opt_type": "PE", "settle": 120.0, "expiry": EXP},
        {"strike": 24300, "opt_type": "PE", "settle": 70.0, "expiry": EXP},
        {"strike": 25500, "opt_type": "CE", "settle": 110.0, "expiry": EXP},
        {"strike": 25700, "opt_type": "CE", "settle": 65.0, "expiry": EXP},
    ]
    iv_df = add_iv(pd.DataFrame(rows), spot=25000, asof=ASOF)
    snap = build_chain_snapshot("NIFTY", 25000, ASOF, EXP, iv_df, iv_history=[0.10, 0.16])
    assert snap["atm_iv"] == pytest.approx(0.15, abs=1e-3)
    assert snap["iv_rank"] > 0.5

    ideas = FnoIncomeEngine(lot_size=50, strike_step=50, source=lambda: [snap]).scan()
    assert len(ideas) == 1
    assert ideas[0].max_loss_rupees == pytest.approx(105 * 50)   # credit 95, width 200
    ideas[0].validate()
