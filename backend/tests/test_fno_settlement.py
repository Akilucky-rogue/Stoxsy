"""Tests for the index settlement source (F&O increment 5b) — how condor
resolution learns the underlying's expiry price. All pure (sources injected)."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.fno.settlement import index_settlement, settlement_from_bhavcopy

EXP = date(2026, 7, 31)
OTHER = date(2026, 8, 28)
HEAD = ("FinInstrmTp,TckrSymb,XpryDt,StrkPric,OptnTp,SttlmPric,OpnIntrst,"
        "UndrlygPric,NewBrdLotQty")


def _csv(xpry="2026-07-31", upx="25123.45"):
    rows = [f"IDO,NIFTY,{xpry},25000,CE,150.0,1000,{upx},75",
            f"IDO,NIFTY,{xpry},25000,PE,140.0,1000,{upx},75"]
    return HEAD + "\n" + "\n".join(rows) + "\n"


def test_settlement_from_bhavcopy_exact_expiry():
    assert settlement_from_bhavcopy(_csv(), "NIFTY", EXP) == pytest.approx(25123.45)


def test_settlement_from_bhavcopy_falls_back_to_any_expiry():
    # file only has the NEXT expiry's rows, but the index level is the same -> use it
    assert settlement_from_bhavcopy(_csv(xpry="2026-08-28"), "NIFTY", EXP) == pytest.approx(25123.45)


def test_settlement_from_bhavcopy_absent_underlying_is_none():
    assert settlement_from_bhavcopy(_csv(), "BANKNIFTY", EXP) is None


def test_index_settlement_prefers_persisted_and_skips_fetch():
    def boom(_d):
        raise AssertionError("must not fetch when persisted answers")
    got = index_settlement("NIFTY", EXP, persisted=lambda u, e: 25000.0, bhavcopy_fetch=boom)
    assert got == pytest.approx(25000.0)


def test_index_settlement_uses_bhavcopy_when_not_persisted():
    got = index_settlement("NIFTY", EXP, persisted=lambda u, e: None,
                           bhavcopy_fetch=lambda d: _csv())
    assert got == pytest.approx(25123.45)


def test_index_settlement_falls_through_to_extra_lookup():
    got = index_settlement("NIFTY", EXP, persisted=lambda u, e: None, fetch=False,
                           extra_lookups=[lambda u, e: None, lambda u, e: 24999.0])
    assert got == pytest.approx(24999.0)


def test_index_settlement_returns_none_when_nothing_answers():
    assert index_settlement("NIFTY", EXP, persisted=lambda u, e: None, fetch=False) is None


def test_index_settlement_tolerates_a_failing_source():
    def boom(_d):
        raise RuntimeError("NSE blocked")
    got = index_settlement("NIFTY", EXP, persisted=lambda u, e: None, bhavcopy_fetch=boom,
                           extra_lookups=[lambda u, e: 24000.0])
    assert got == pytest.approx(24000.0)
